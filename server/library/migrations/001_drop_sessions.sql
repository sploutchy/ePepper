-- Web-app auth moved from a DB-backed session table to a stateless
-- cookie (an HMAC of the API key, validated by recomputation). Drop the
-- now-unused table; no data worth keeping ever lived here.
DROP TABLE IF EXISTS sessions;
