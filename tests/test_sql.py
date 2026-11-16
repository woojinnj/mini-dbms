import unittest

from minidb.errors import ParseError
from minidb.sql import Insert, MAX_SQL_BYTES, parse


class ParserTests(unittest.TestCase):
    def test_multiline_comments_quotes_unicode(self):
        statements = parse("""-- heading
        iNsErT INTO USERS (name, age, email) VALUES
        ('한글; -- /* */ O''Brien', 20, NULL), ('다음', -1, 'x');
        /* block comment */ SELECT name FROM users WHERE id >= 1 AND email IS NOT NULL LIMIT 2;
        """)
        self.assertIsInstance(statements[0], Insert)
        self.assertEqual(statements[0].rows[0][0], "한글; -- /* */ O'Brien")
        self.assertEqual(statements[0].table, "users")
        self.assertEqual(statements[1].conditions[1].operator, "IS NOT")

    def test_count_explain_and_literals(self):
        query = parse("EXPLAIN SELECT COUNT(*) FROM users WHERE id != 3 LIMIT 0")[0]
        self.assertTrue(query.explain)
        self.assertTrue(query.count)
        self.assertEqual(query.limit, 0)
        self.assertEqual(parse("INSERT INTO t VALUES (TRUE, FALSE, -1.25, '')")[0].rows,
                         ((True, False, -1.25, ""),))

    def test_reject_invalid_and_unsupported_syntax(self):
        statements = ["", "-- only comment", "SELECT", "SELECT * FROM", "DELETE FROM users",
                      "SELECT * FROM users WHERE id =", "SELECT * FROM users WHERE id = 'x",
                      "SELECT * FROM users WHERE id = 1 OR id = 2", "SELECT * FROM users LIMIT -1",
                      "SELECT * FROM users LIMIT TRUE", "SELECT * FROM users LIMIT 1.5",
                      "SELECT * FROM users; /* unclosed", "INSERT INTO t VALUES ()",
                      "SELECT * FROM users SELECT * FROM users", "SELECT * FROM ../../etc/passwd",
                      "SELECT * FROM users WHERE id == 1", "SELECT * FROM users WHERE name = 'a' garbage"]
        for sql in statements:
            with self.subTest(sql=sql), self.assertRaises(ParseError):
                parse(sql)

    def test_limits(self):
        with self.assertRaises(ParseError):
            parse(" " * (MAX_SQL_BYTES + 1))
        with self.assertRaises(ParseError):
            parse("INSERT INTO t VALUES " + ",".join("(1)" for _ in range(10_001)))
        with self.assertRaises(ParseError):
            parse("INSERT INTO t VALUES (" + "9" * 5000 + ")")

    def test_keyword_strings_are_literals(self):
        sql = "INSERT INTO users VALUES ('NULL', 1, 'TRUE'); SELECT * FROM users WHERE name = 'AND'"
        self.assertEqual(parse(sql)[0].rows[0], ("NULL", 1, "TRUE"))

    def test_count_column_and_invalid_unicode(self):
        self.assertEqual(parse("SELECT count FROM inventory")[0].columns, ("count",))
        with self.assertRaises(ParseError):
            parse("SELECT '\ud800' FROM users")
