-- ID is assigned by the engine. Escape a quote using two quotes.
INSERT INTO users (name, age, email) VALUES
  ('김정글', 25, 'jungle@example.com'),
  ('O''Brien; SQL 문자열', 31, NULL),
  ('박개발', 28, 'dev@example.com');
SELECT * FROM users;
SELECT name, age FROM users WHERE id = 2;
SELECT * FROM users WHERE age >= 26 AND email IS NOT NULL;
SELECT * FROM users WHERE id >= 1 AND id < 3 LIMIT 10;
SELECT COUNT(*) FROM users;
EXPLAIN SELECT * FROM users WHERE id = 2;
EXPLAIN SELECT * FROM users WHERE age = 31;
