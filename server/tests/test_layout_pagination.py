"""Instruction pagination: long steps split across pages instead of
moving wholesale to the next page and leaving a blank band at the
bottom of the column."""

from rendering.layout import _MIN_SPLIT_LINES, _paginate_instructions

LH = 18


def _step(n: int) -> dict:
    return {"type": "step", "lines": [f"l{i}" for i in range(n)], "line_h": LH}


def _heading() -> dict:
    return {"type": "heading", "lines": ["H"], "line_h": 20}


def _block_h(b: dict) -> int:
    extra = 14 if b["type"] == "heading" else 6
    return len(b["lines"]) * b["line_h"] + extra


def _lines_per_page(pages):
    return [sum(e - s for _, s, e in p) for p in pages]


def test_long_step_splits_to_fill_page():
    blocks = [_step(5), _step(8)]
    groups = [[0], [1]]
    available = 9 * LH + 6  # 5 lines + gap, then room for 4 more lines
    pages = _paginate_instructions(blocks, groups, available, _block_h)
    assert pages[0] == [(0, 0, 5), (1, 0, 4)]
    assert pages[1] == [(1, 4, 8)]


def test_no_orphan_or_widow_lines():
    blocks = [_step(5), _step(3)]
    groups = [[0], [1]]
    # Room for 2 lines of the 3-line step would leave a 1-line widow.
    available = 7 * LH + 6
    pages = _paginate_instructions(blocks, groups, available, _block_h)
    assert pages == [[(0, 0, 5)], [(1, 0, 3)]]
    for p in pages:
        for k, s, e in p:
            n = len(blocks[k]["lines"])
            if (s, e) != (0, n):
                assert e - s >= _MIN_SPLIT_LINES


def test_heading_stays_with_first_slice():
    blocks = [_step(6), _heading(), _step(6)]
    groups = [[0], [1, 2]]
    available = 6 * LH + 6 + _block_h(blocks[1]) + 3 * LH
    pages = _paginate_instructions(blocks, groups, available, _block_h)
    assert pages[0] == [(0, 0, 6), (1, 0, 1), (2, 0, 3)]
    assert pages[1] == [(2, 3, 6)]


def test_step_taller_than_page_is_split_and_complete():
    blocks = [_step(30)]
    pages = _paginate_instructions(blocks, [[0]], 10 * LH, _block_h)
    assert _lines_per_page(pages) == [10, 10, 10]


def test_short_recipe_single_page_unchanged():
    blocks = [_step(2), _step(3)]
    pages = _paginate_instructions(blocks, [[0], [1]], 300, _block_h)
    assert pages == [[(0, 0, 2), (1, 0, 3)]]
