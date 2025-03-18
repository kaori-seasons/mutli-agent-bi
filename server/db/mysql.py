import pymysql.cursors
import os


def get_conn(host,port,user, pwd, db_name):
    connection = pymysql.connect(host=host,
                                 port=port,
                                 user=user,
                                 password=pwd,
                                 database=db_name)
    return connection


def get_binlog_info(conn):
    with conn.cursor() as cursor:
        sql = 'show master status;'
        cursor.execute(sql)
        conn.commit()
        t = cursor.fetchone()
        return t['File'], t['Position']


def fetch_one(sql: str, conn):
    with conn.cursor() as cursor:
        cursor.execute(sql)
        conn.commit()
        t = cursor.fetchone()
        return t


def fetch(sql: str, conn):
    with conn.cursor() as cursor:
        cursor.execute(sql)
        conn.commit()
        t = cursor.fetchall()
        return t, cursor.rowcount
