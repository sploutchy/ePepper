-- Replace freeform comments with a first-class tags column.
--
-- 1. Add tags TEXT to recipes (comma-separated lowercase tag list).
-- 2. Drop the comments table entirely.
-- 3. Drop the FTS index — init_db will recreate it with a `tags` column
--    in place of the old `notes` column.
-- 4. Clear the fts_rebuilt sentinel so init_db rebuilds the index from
--    the new schema on the next startup.

ALTER TABLE recipes ADD COLUMN tags TEXT;

DROP TABLE IF EXISTS comments;
DROP INDEX IF EXISTS idx_comments_recipe_id;

DROP TABLE IF EXISTS recipes_fts;
DELETE FROM meta WHERE key = 'fts_rebuilt';
