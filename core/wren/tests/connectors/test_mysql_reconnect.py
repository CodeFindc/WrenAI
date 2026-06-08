import sys
from unittest.mock import MagicMock, patch

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
from wren.connector.mysql import MySqlConnector, DorisConnector
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

    # Initialize connector
    connector = MySqlConnector(mock_connection_info)

    assert connector._connection_info == mock_connection_info
    mock_connect.assert_called_once()
    mock_cursor.execute.assert_called_with("SET sql_mode=CONCAT(@@sql_mode, ',ANSI_QUOTES')")

    # Run query
    mock_cursor.description = [("col1", 253, None, None, None, None, None)] # 253 is VARCHAR/STRING
    mock_cursor.fetchall.return_value = [("val1",)]

    tbl = connector.query("SELECT * FROM test")
    
    assert isinstance(tbl, pa.Table)
    mock_conn.ping.assert_called_with()


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

    # Connection 1 fails ping
    mock_conn1.ping.side_effect = Exception("Connection lost")

    # Initialize connector (first connection)
    connector = MySqlConnector(mock_connection_info)
    assert connector.connection == mock_conn1

    # Run query (should trigger reconnect)
    mock_cursor2.description = [("col1", 253, None, None, None, None, None)]
    mock_cursor2.fetchall.return_value = [("val1",)]

    connector.query("SELECT * FROM test")

    # Should attempt to close connection 1
    mock_conn1.close.assert_called()
    # Connection should now be connection 2
    assert connector.connection == mock_conn2
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

    # Query execution on cursor 1 raises 2006
    mock_cursor1.execute.side_effect = [None, MockOperationalError(2006, "Server has gone away")]

    # Initialize connector
    connector = MySqlConnector(mock_connection_info)

    # Run query
    mock_cursor2.description = [("col1", 253, None, None, None, None, None)]
    mock_cursor2.fetchall.return_value = [("val2",)]

    connector.query("SELECT * FROM test")

    # Should close connection 1 and reconnect to connection 2
    mock_conn1.close.assert_called()
    assert connector.connection == mock_conn2
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
    assert connector.connection == mock_conn1

    # Doris connector skips ANSI_QUOTES in init, verify it didn't call it on connection 1
    mock_cursor1.execute.assert_not_called()

    # Trigger reconnect by failing ping
    mock_conn1.ping.side_effect = Exception("Connection lost")
    mock_cursor2.description = [("col1", 253, None, None, None, None, None)]
    mock_cursor2.fetchall.return_value = [("val1",)]

    connector.query("SELECT * FROM test")

    assert connector.connection == mock_conn2
    # Verify it did NOT execute SET sql_mode on connection 2 either
    mock_cursor2.execute.assert_called_once_with("SELECT * FROM test")
