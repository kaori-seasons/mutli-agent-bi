import pandas as pd
import os
import json
from pysqler import Insert

def load_from_excel(doc_path, table_name, conn):
    df = pd.read_excel(doc_path, sheet_name=0)

 
    for i in range(0, len(df)):
        command = Insert("`{0}`".format(table_name))
        for column_name in df.columns:
            v = df.loc[i, column_name]
            if not pd.isna(v):
                command.put(column_name, df.loc[i, column_name])
            else:
                command.put(column_name, None)

        sql = str(command)

        with conn.cursor() as cursor:
            sql = str(command)
            cursor.execute(sql)
            conn.commit()
        
        
        
