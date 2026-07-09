-- Drop the cook-count column. touch_displayed() now only bumps
-- last_displayed_at; the "most/least cooked" sorts and "cooked N×"
-- badges in the web UI have been removed.
ALTER TABLE recipes DROP COLUMN displayed_count;
