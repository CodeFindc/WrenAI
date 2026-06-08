import sys
from unittest.mock import MagicMock, patch
import queue
import threading
import time

# Define custom OperationalError mock class so it behaves like the real one
class MockOperationalError(Exception):
    def __init__(self, *args):
        super().__init__(*args)
        self.args = args

# Pre-mock MySQLdb module before importing connector
mock_mysqldb = MagicMock()
mock_mysqldb.OperationalError = MockOperationalError
sys.modules["MySQLdb"] = mock_mysqldb

# Pre-mock MySQLdb.constants module
mock_constants = MagicMock()
sys.modules["MySQLdb.constants"] = mock_constants

import pytest
import pyarrow as pa
from wren.connector.mysql import MySqlConnector, DorisConnector, ConnectionPool
from wren.model import MySqlConnectionInfo


@pytest.fixture
def mock_connection_info():
    return MySqlConnectionInfo(
        host="localhost",
        port=3306,
        database="test_db",
        user="test_user",
        password="test_password",
        sslMode="disabled",
    )


@patch("MySQLdb.connect")
def test_mysql_connector_init_and_query_success(mock_connect, mock_connection_info):
    # Setup mocks
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_connect.return_value = mock_conn
    mock_conn.cursor.return_value = mock_cursor

    # Initialize connector — should NOT call MySQLdb.connect yet (lazy initialization)
    connector = MySqlConnector(mock_connection_info)
    assert connector._connection_info == mock_connection_info
    mock_connect.assert_not_called()

    # Run query — this should pull a connection and initialize it
    mock_cursor.description = [("col1", 253, None, None, None, None, None)]  # 253 is VARCHAR/STRING
    mock_cursor.fetchall.return_value = [("val1",)]

    tbl = connector.query("SELECT * FROM test")
    
    assert isinstance(tbl, pa.Table)
    mock_connect.assert_called_once()
    mock_cursor.execute.assert_any_call("SET sql_mode=CONCAT(@@sql_mode, ',ANSI_QUOTES')")
    mock_cursor.execute.assert_any_call("SELECT * FROM test")


@patch("MySQLdb.connect")
def test_mysql_connector_ping_fails_reconnects(mock_connect, mock_connection_info):
    # Setup mock connections
    mock_conn1 = MagicMock()
    mock_conn2 = MagicMock()
    mock_cursor1 = MagicMock()
    mock_cursor2 = MagicMock()

    mock_connect.side_effect = [mock_conn1, mock_conn2]
    mock_conn1.cursor.return_value = mock_cursor1
    mock_conn2.cursor.return_value = mock_cursor2

    # Initialize connector
    connector = MySqlConnector(mock_connection_info)

    # First query to establish the first connection
    mock_cursor1.description = [("col1", 253, None, None, None, None, None)]
    mock_cursor1.fetchall.return_value = [("val1",)]
    connector.query("SELECT * FROM test")
    mock_connect.assert_called_once()

    # Connection 1 now fails ping during the second borrow
    mock_conn1.ping.side_effect = Exception("Connection lost")

    # Run query again (should trigger reconnect in get_connection)
    mock_cursor2.description = [("col1", 253, None, None, None, None, None)]
    mock_cursor2.fetchall.return_value = [("val2",)]
    connector.query("SELECT * FROM test")

    # Should attempt to close connection 1
    mock_conn1.close.assert_called()
    # MySQLdb.connect should have been called twice now
    assert mock_connect.call_count == 2
    # Connection 2 should have sql_mode initialized
    mock_cursor2.execute.assert_any_call("SET sql_mode=CONCAT(@@sql_mode, ',ANSI_QUOTES')")
    # Connection 2 should execute the query
    mock_cursor2.execute.assert_any_call("SELECT * FROM test")


@patch("MySQLdb.connect")
def test_mysql_connector_query_raises_2006_retries(mock_connect, mock_connection_info):
    mock_conn1 = MagicMock()
    mock_conn2 = MagicMock()
    mock_cursor1 = MagicMock()
    mock_cursor2 = MagicMock()

    mock_connect.side_effect = [mock_conn1, mock_conn2]
    mock_conn1.cursor.return_value = mock_cursor1
    mock_conn2.cursor.return_value = mock_cursor2

    # Initialize connector
    connector = MySqlConnector(mock_connection_info)

    # Query execution on cursor 1:
    # First execute is for SET sql_mode=... (should succeed),
    # Second execute is for SELECT * FROM test (should raise 2006 to trigger retry).
    mock_cursor1.execute.side_effect = [None, MockOperationalError(2006, "Server has gone away")]

    # Run query
    mock_cursor2.description = [("col1", 253, None, None, None, None, None)]
    mock_cursor2.fetchall.return_value = [("val2",)]

    connector.query("SELECT * FROM test")

    # Should close connection 1 and reconnect to connection 2
    mock_conn1.close.assert_called()
    # MySQLdb.connect should have been called twice (one for first try, one for retry)
    assert mock_connect.call_count == 2
    # Connection 2 should execute the query
    mock_cursor2.execute.assert_any_call("SELECT * FROM test")


@patch("MySQLdb.connect")
def test_doris_connector_reconnect_no_ansi_quotes(mock_connect, mock_connection_info):
    mock_conn1 = MagicMock()
    mock_conn2 = MagicMock()
    mock_cursor1 = MagicMock()
    mock_cursor2 = MagicMock()

    mock_connect.side_effect = [mock_conn1, mock_conn2]
    mock_conn1.cursor.return_value = mock_cursor1
    mock_conn2.cursor.return_value = mock_cursor2

    # Initialize Doris connector
    connector = DorisConnector(mock_connection_info)

    # First query
    mock_cursor1.description = [("col1", 253, None, None, None, None, None)]
    mock_cursor1.fetchall.return_value = [("val1",)]
    connector.query("SELECT * FROM test")

    # Doris connector skips ANSI_QUOTES in init, verify it didn't call it on connection 1
    mock_cursor1.execute.assert_called_once_with("SELECT * FROM test")

    # Trigger reconnect by failing ping
    mock_conn1.ping.side_effect = Exception("Connection lost")
    mock_cursor2.description = [("col1", 253, None, None, None, None, None)]
    mock_cursor2.fetchall.return_value = [("val2",)]

    connector.query("SELECT * FROM test")

    # Verify it did NOT execute SET sql_mode on connection 2 either
    mock_cursor2.execute.assert_called_once_with("SELECT * FROM test")


def test_connection_pool_lazy_and_limit():
    creator_calls = 0
    conns_created = []

    def fake_creator():
        nonlocal creator_calls
        creator_calls += 1
        conn = MagicMock()
        conns_created.append(conn)
        return conn

    # Pool with max size = 2
    pool = ConnectionPool(creator=fake_creator, max_connections=2)

    # Initial state
    assert creator_calls == 0

    # 1. Get first connection
    c1 = pool.get_connection()
    assert creator_calls == 1
    assert c1 == conns_created[0]

    # 2. Get second connection
    c2 = pool.get_connection()
    assert creator_calls == 2
    assert c2 == conns_created[1]

    # 3. Getting third connection should block. We will run it in a separate thread.
    third_conn_acquired = []
    
    def acquire_third():
        c3 = pool.get_connection()
        third_conn_acquired.append(c3)
        pool.return_connection(c3)

    t = threading.Thread(target=acquire_third)
    t.start()

    time.sleep(0.1)
    # Since pool is full and connections are not returned, thread must still be waiting
    assert len(third_conn_acquired) == 0
    assert creator_calls == 2

    # Now return one connection
    pool.return_connection(c1)
    
    # Wait for the thread to wake up and acquire it
    t.join(timeout=1.0)
    
    assert len(third_conn_acquired) == 1
    assert third_conn_acquired[0] == c1
    # Creator should not have been called since we reused c1
    assert creator_calls == 2

    pool.close()
