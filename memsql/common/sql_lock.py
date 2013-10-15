from memsql.common.connection_pool import ConnectionPool, MySQLError
from memsql.common import errorcodes
import time
import uuid

LOCK_TABLE = """\
CREATE TABLE IF NOT EXISTS %(name)s (
    id VARCHAR(255) PRIMARY KEY,
    lock_hash BINARY(32),
    owner VARCHAR(1024),
    last_contact TIMESTAMP DEFAULT NOW(),
    expiry INT
)"""

class NotConnected(Exception):
    pass

class SQLLockManager(object):
    def __init__(self, table_prefix="sqllock"):
        """ Initialize the SQLLockManager with the specified table prefix.
        """
        self.table_name = table_prefix.rstrip('_') + '_locks'
        self._pool = ConnectionPool()
        self._db_args = None

    ###############################
    # Public Interface

    def connect(self, host='127.0.0.1', port=3306, user='root', password='', database=''):
        """ Connect to the database specified """
        self._db_args = { 'host': host, 'port': port, 'user': user, 'password': password, 'database': database }
        with self._db_conn() as conn:
            conn.query('SELECT 1')
        return self

    def setup(self):
        """ Initialize the required tables in the database """
        with self._db_conn() as conn:
            conn.execute(LOCK_TABLE % { 'name': self.table_name })
        return self

    def destroy(self):
        """ Destroy the SQLStepQueue tables in the database """
        with self._db_conn() as conn:
            conn.execute('DROP TABLE IF EXISTS %s' % self.table_name)
        return self

    def ready(self):
        """ Returns True if the tables have been setup, False otherwise """
        with self._db_conn() as conn:
            tables = [row.t for row in conn.query('''
                SELECT table_name AS t FROM information_schema.tables
                WHERE table_schema=%s
            ''', self._db_args['database'])]
        return self.table_name in tables

    def acquire(self, lock_id, owner='', expiry=5 * 60, block=False, timeout=None, retry_interval=0.5):
        start = time.time()
        while 1:
            lock_ref = self._acquire_lock(lock_id, owner, expiry)
            if lock_ref is None and block:
                if timeout is not None and (time.time() - start) > timeout:
                    break
                time.sleep(retry_interval)
            else:
                break
        return lock_ref

    ###############################
    # Private Interface

    def _db_conn(self):
        if self._db_args is None:
            raise NotConnected()
        return self._pool.connect(**self._db_args)

    def _acquire_lock(self, lock_id, owner, expiry):
        try:
            with self._db_conn() as conn:
                conn.execute('''
                    DELETE FROM %(table_name)s
                    WHERE last_contact <= NOW() - expiry
                ''' % { 'table_name': self.table_name })

                lock_hash = uuid.uuid1().hex
                conn.execute('''
                    INSERT INTO %s (id, lock_hash, owner, expiry)
                    VALUES (%%s, %%s, %%s, %%s)
                ''' % self.table_name, lock_id, lock_hash, owner, expiry)

                return SQLLock(lock_id=lock_id, lock_hash=lock_hash, owner=owner, manager=self)

        except MySQLError as (errno, msg):
            if errno == errorcodes.ER_DUP_ENTRY:
                return None
            else:
                raise

class SQLLock(object):
    def __init__(self, lock_id, lock_hash, owner, manager):
        self._manager = manager
        self._lock_id = lock_id
        self._lock_hash = lock_hash

        self.owner = owner

    ###############################
    # Public Interface

    def valid(self):
        with self._db_conn() as conn:
            row = conn.get('''
                SELECT
                    (lock_hash=%%s && last_contact > NOW() - expiry) AS valid
                FROM %s WHERE id = %%s
            ''' % self._manager.table_name, self._lock_hash, self._lock_id)

        return bool(row is not None and row.valid)

    def ping(self):
        """ Notify the manager that this lock is still active. """
        with self._db_conn() as conn:
            affected_rows = conn.query('''
                UPDATE %s
                SET last_contact=NOW()
                WHERE id = %%s AND lock_hash = %%s
            ''' % self._manager.table_name, self._lock_id, self._lock_hash)

        return bool(affected_rows == 1)

    def release(self):
        """ Release the lock. """
        if self.valid():
            with self._db_conn() as conn:
                affected_rows = conn.query('''
                    DELETE FROM %s
                    WHERE id = %%s AND lock_hash = %%s
                ''' % self._manager.table_name, self._lock_id, self._lock_hash)
            return bool(affected_rows == 1)
        else:
            return False

    ###############################
    # Context Management

    def __enter__(self):
        return self

    def __exit__(self, *args, **kwargs):
        self.release()

    ###############################
    # Public Interface

    def _db_conn(self):
        return self._manager._db_conn()
