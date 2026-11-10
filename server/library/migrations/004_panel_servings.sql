-- Remember the serving count a recipe was pushed at, so a container
-- restart re-renders the panel with the same scaled quantities instead
-- of silently reverting to the recipe's own count. NULL = as written.

ALTER TABLE display_panel ADD COLUMN servings INTEGER;
