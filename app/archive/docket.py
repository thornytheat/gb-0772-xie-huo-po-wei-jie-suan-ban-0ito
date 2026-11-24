from contextlib import closing,contextmanager
import json
from pathlib import Path
import secrets
import sqlite3

from app.archive.canonical import serialize
from app.problems import DocketError


class Docket:
    def __init__(self,path):
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as database:
            database.executescript('''
                CREATE TABLE IF NOT EXISTS settings(name TEXT PRIMARY KEY, value BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS dockets(id TEXT PRIMARY KEY, content TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS rulings(dispute_key TEXT PRIMARY KEY, content TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS supplements(ruling_key TEXT PRIMARY KEY, content TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS awards(minute_key TEXT PRIMARY KEY, dispute_key TEXT NOT NULL);
            ''')
            database.execute("INSERT OR IGNORE INTO settings VALUES ('pseudonym_key', ?)",(secrets.token_bytes(32),))
            self.secret=database.execute("SELECT value FROM settings WHERE name='pseudonym_key'").fetchone()[0]

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path,timeout=15)) as database,database:
            yield database

    def find(self,key):
        with self.connect() as database:
            row=database.execute('SELECT content FROM dockets WHERE id=?',(key,)).fetchone()
        return None if row is None else json.loads(row[0])

    def require(self,key):
        packet=self.find(key)
        if packet is None:
            raise DocketError('DOCKET_MISSING','清算留档不存在',404)
        return packet

    def publish(self,key,packet):
        content=serialize(packet)
        with self.connect() as db:
            inserted=db.execute('INSERT OR IGNORE INTO dockets VALUES (?,?)',(key,content)).rowcount==1
            original=db.execute('SELECT content FROM dockets WHERE id=?',(key,)).fetchone()[0]
        return json.loads(original),not inserted

    def manifests(self):
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute(
                "SELECT json_extract(content,'$.manifest') FROM dockets ORDER BY rowid DESC LIMIT 100")]

    def rule(self,ruling,supplement,awards):
        content=serialize(ruling)
        with closing(sqlite3.connect(self.path,timeout=15,isolation_level=None)) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                inserted=db.execute('INSERT OR IGNORE INTO rulings VALUES (?,?)',
                                    (ruling['dispute_key'],content)).rowcount==1
                original=db.execute('SELECT content FROM rulings WHERE dispute_key=?',
                                    (ruling['dispute_key'],)).fetchone()[0]
                winner=json.loads(original)
                if not inserted and (winner['vehicle'],winner['shift'])!=(ruling['vehicle'],ruling['shift']):
                    raise DocketError('RULING_CONFLICT',f"该争议已有{winner['shift']}班生效裁定",409)
                for key in awards:
                    db.execute('INSERT OR IGNORE INTO awards VALUES (?,?)',(key,ruling['dispute_key']))
                    holder=db.execute('SELECT dispute_key FROM awards WHERE minute_key=?',(key,)).fetchone()[0]
                    if holder!=ruling['dispute_key']:
                        raise DocketError('RULING_CONFLICT','同车同刻另一泊位已裁定计费，放行以避免多收',409)
                db.execute('INSERT OR IGNORE INTO supplements VALUES (?,?)',
                           (ruling['ruling_key'],serialize(supplement)))
                posted=db.execute('SELECT content FROM supplements WHERE ruling_key=?',
                                  (ruling['ruling_key'],)).fetchone()[0]
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK')
                raise
        return winner,json.loads(posted),not inserted

    def rulings_for(self,sample):
        with self.connect() as db:
            rows=db.execute('SELECT content FROM rulings ORDER BY rowid').fetchall()
        return [ruling for (text,) in rows if (ruling:=json.loads(text))['sample']==sample]
