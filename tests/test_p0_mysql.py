"""Opt-in real MySQL tests. Use ONLY the documented disposable container."""
import os
import time
import unittest
from uuid import uuid4

import pymysql

from server.db.mysql import execute_read
from server.query_policy import PreparedQuery, bind_template
from server.execution import execute_many, QueryBudget
from server.federation import merge_results, validate_merge_query
from server.errors import ServiceError


@unittest.skipUnless(os.getenv("P0_MYSQL_TEST_PORT") and os.getenv("P0_MYSQL_TEST_ALLOW_CREATE") == "1",
                     "disposable MySQL fixture not enabled")
class MySQLIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = int(os.environ["P0_MYSQL_TEST_PORT"])
        cls.suffix = uuid4().hex[:10]
        cls.databases = ["p0_a_" + cls.suffix, "p0_b_" + cls.suffix]
        cls.users = ["p0_ra_" + cls.suffix, "p0_rb_" + cls.suffix]
        cls.admin = pymysql.connect(host="127.0.0.1", port=cls.port, user="root",
                                    password=os.environ["P0_MYSQL_TEST_PASSWORD"], autocommit=True)
        cls.addClassCleanup(cls.cleanup)
        cls.infos = []
        with cls.admin.cursor() as cursor:
            for index, (database, user) in enumerate(zip(cls.databases, cls.users)):
                cursor.execute(f"CREATE DATABASE {database}")
                cursor.execute(f"CREATE USER '{user}'@'%' IDENTIFIED BY 'synthetic-reader-only'")
                cursor.execute(f"GRANT SELECT ON {database}.* TO '{user}'@'%'")
                cursor.execute(f"CREATE TABLE {database}.orders (id INT PRIMARY KEY, city VARCHAR(80), amount DECIMAL(12,2))")
                count = 1 if index == 0 else 9
                cursor.executemany(f"INSERT INTO {database}.orders VALUES (%s,%s,%s)",
                                   [(i, "上海", 100 if index == 0 else 0) for i in range(count)])
                cursor.execute(f"CREATE TABLE {database}.lookup (value VARCHAR(200))")
                cursor.executemany(f"INSERT INTO {database}.lookup VALUES (%s)",
                                   [("plain",), ("x' OR 1=1; DROP TABLE orders --",)])
                cursor.execute(f"CREATE TABLE {database}.numbers (n INT)")
                cursor.executemany(f"INSERT INTO {database}.numbers VALUES (%s)", [(i,) for i in range(1000)])
                cls.infos.append(dict(source_id="a" if index == 0 else "b", host="127.0.0.1",
                                      port=cls.port, user=user, pwd="synthetic-reader-only", db=database, desc=database))

    @classmethod
    def cleanup(cls):
        try:
            with cls.admin.cursor() as cursor:
                for database, user in zip(cls.databases, cls.users):
                    cursor.execute(f"DROP DATABASE IF EXISTS {database}")
                    cursor.execute(f"DROP USER IF EXISTS '{user}'@'%'")
        finally:
            cls.admin.close()

    def run_query(self, query, **kwargs):
        return execute_read(self.infos[0], query, timeout=3, max_rows=100, max_bytes=1048576, **kwargs)

    def test_real_binding_and_percent_format(self):
        value = "x' OR 1=1; DROP TABLE orders --"
        query = bind_template("SELECT value, DATE_FORMAT('2026-11-25','%Y') yr FROM lookup WHERE value='{0}'", [value])
        output = self.run_query(query)
        self.assertEqual(output["rows"], [(value, "2026")])
        self.assertEqual(self.run_query(PreparedQuery("SELECT COUNT(*) n FROM orders"))["rows"][0][0], 1)

    def test_cte_and_bounded_streaming(self):
        query = PreparedQuery("WITH q AS (SELECT n FROM numbers) SELECT n FROM q ORDER BY n")
        output = execute_read(self.infos[0], query, timeout=3, max_rows=2, max_bytes=1024)
        self.assertEqual(output["rows"], [(0,), (1,)])
        self.assertTrue(output["truncated"])

    def test_source_accounts_cannot_write_or_read_other_database(self):
        info = self.infos[0]
        connection = pymysql.connect(host=info["host"], port=info["port"], user=info["user"],
                                     password=info["pwd"], database=info["db"])
        try:
            with connection.cursor() as cursor:
                with self.assertRaises(pymysql.MySQLError):
                    cursor.execute("INSERT INTO orders VALUES (999,'x',1)")
                with self.assertRaises(pymysql.MySQLError):
                    cursor.execute(f"SELECT * FROM {self.databases[1]}.orders")
        finally:
            connection.close()

    def test_transaction_itself_is_read_only(self):
        # This uses the fixture admin to distinguish transaction protection from grants.
        with self.admin.cursor() as cursor:
            cursor.execute("START TRANSACTION READ ONLY")
            try:
                with self.assertRaises(pymysql.MySQLError):
                    cursor.execute(f"INSERT INTO {self.databases[0]}.orders VALUES (999,'x',1)")
            finally:
                self.admin.rollback()

    def test_mysql_server_interrupts_expensive_select(self):
        start = time.monotonic()
        with self.assertRaises((pymysql.MySQLError, ServiceError)):
            execute_read(self.infos[0], PreparedQuery(
                "SELECT SUM(a.n+b.n+c.n) total FROM numbers a CROSS JOIN numbers b CROSS JOIN numbers c"),
                timeout=0.05, max_rows=10, max_bytes=1024)
        self.assertLess(time.monotonic() - start, 3)

    def test_real_cross_database_weighted_average(self):
        query = PreparedQuery("SELECT city,SUM(amount) amount,COUNT(amount) n FROM orders GROUP BY city")
        spec = {"version": 1, "sources": ["a", "b"], "disjoint_partitions": True,
                "dimensions": ["city"],
                "measures": {"average": {"op": "weighted_avg", "numerator": "amount", "denominator": "n"}}}
        for info in self.infos:
            validate_merge_query(query, spec, info["db"])
        output = merge_results(execute_many(self.infos, query, QueryBudget()), spec)
        self.assertEqual(output["rows"], [("上海", 10)])

    def test_connection_failure_cannot_return_complete_total(self):
        query = PreparedQuery("SELECT SUM(amount) amount FROM orders")
        spec = {"version": 1, "sources": ["a", "b"], "disjoint_partitions": True,
                "dimensions": [], "measures": {"total": {"op": "sum", "input": "amount"}}}
        infos = [self.infos[0], dict(self.infos[1], pwd="wrong-password")]
        output = execute_many(infos, query, QueryBudget())
        self.assertEqual(output[1]["status"], "failed")
        with self.assertRaises(ServiceError) as error:
            merge_results(output, spec)
        self.assertEqual(error.exception.code, "PARTIAL_RESULT")


    def test_p1_published_weighted_template_runs_through_p0_gateway(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from tests.test_registry_v2 import document, AUTHOR, REVIEWER
        from ontology.registry import Registry
        from server import api, conf
        doc = document(['a', 'b'])
        for e in doc['entities']:
            if e['kind'] == 'DataSource':
                e['attributes']['database'] = next(i['db'] for i in self.infos if i['source_id'] == e['source_ids'][0])
        template = doc['entities'][-1]['attributes']
        template.update(sql="SELECT city, SUM(amount) amount_sum, COUNT(amount) amount_count FROM orders WHERE city='{0}' GROUP BY city",
                        outputs=['city', 'amount_sum', 'amount_count'], column_types=['维度', '度量', '度量'],
                        merge={'version': 1, 'sources': ['a', 'b'], 'disjoint_partitions': True, 'dimensions': ['city'],
                               'measures': {'average': {'op': 'weighted_avg', 'numerator': 'amount_sum', 'denominator': 'amount_count'}}})
        infos = {i['source_id']: i for i in self.infos}
        with tempfile.TemporaryDirectory() as path, patch.dict(os.environ, {
            'ONTOLOGY_REGISTRY_PATH': str(Path(path) / 'registry.sqlite3'), 'ONTOLOGY_REGISTRY_NAMESPACE': 'default',
            'ONTOLOGY_QUERY_SOURCES': '["a","b"]'}), patch.dict(conf.mysql_info, infos, clear=True), \
                patch.object(api.aws, 'get', side_effect=AssertionError('unexpected AWS call')):
            registry = Registry()
            change = registry.propose(doc, AUTHOR, None, 0, infos)
            registry.review(change['id'], REVIEWER, change['digest'], 'approved', 'synthetic weighted average reviewed')
            release = registry.publish(change['id'], AUTHOR, change['digest'], infos)['release_id']
            output = api.get_result([{'role': 'user', 'content': '上海销售额'}], 'p1-real-db', principal=AUTHOR, source_ids=['a','b'])
            self.assertEqual(output['ontology']['release_id'], release)
            self.assertIn('| 上海 | 10', output['mdData'])
            self.assertEqual(len(output['sources']), 2)


if __name__ == "__main__":
    unittest.main()
