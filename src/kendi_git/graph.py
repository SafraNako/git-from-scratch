"""``log --graph``: a port of git's graph.c (without colours).

The graph is drawn one text row at a time. For every commit :class:`Graph` keeps the *columns* (the commits that the branch lines in the current row are waiting for)
and works out the columns of the next row (``new_columns``) together with a ``mapping`` that says where each line has to move; rows that only move lines are the
"collapsing" rows (``|/``), a merge gets a "post merge" row (``|\\``), a commit with three or more parents is preceded by extra "pre commit" rows.
"""
from __future__ import annotations

PADDING, SKIP, PRE_COMMIT, COMMIT, POST_MERGE, COLLAPSING = range(6)
MERGE_CHARS = "/|\\"


class Graph:
    def __init__(self):
        self.commit = None
        self.parents = []
        self.num_parents = 0
        self.width = 0
        self.expansion_row = 0
        self.state = PADDING
        self.prev_state = PADDING
        self.commit_index = 0
        self.prev_commit_index = 0
        self.merge_layout = 0
        self.edges_added = 0
        self.prev_edges_added = 0
        self.columns = []                 # commits the lines of the current row wait for
        self.new_columns = []
        self.mapping = []
        self.old_mapping = []
        self.mapping_size = 0

    # --- bookkeeping -------------------------------------------------------------------------------------------------------------------------------
    def _update_state(self, state):
        self.prev_state = self.state
        self.state = state

    def _num_expansion_rows(self):
        return (self.num_parents - 2) * 2                 # two rows for every parent beyond the second

    def _needs_pre_commit_line(self):
        return self.num_parents >= 3 and self.commit_index < len(self.columns) - 1 and self.expansion_row < self._num_expansion_rows()

    def _find_new_column(self, commit):
        for i, c in enumerate(self.new_columns):
            if c == commit:
                return i
        return -1

    def _insert_into_new_columns(self, commit, idx):
        i = self._find_new_column(commit)
        if i < 0:
            i = len(self.new_columns)
            self.new_columns.append(commit)
        if self.num_parents > 1 and idx > -1 and self.merge_layout == -1:
            # the first parent of a merge: choose the layout of the merge line by whether that parent sits left of the merge
            dist = idx - i
            shift = 2 * dist - 3 if dist > 1 else 1
            self.merge_layout = 0 if dist > 0 else 1
            self.edges_added = self.num_parents + self.merge_layout - 2
            mapping_idx = self.width + (self.merge_layout - 1) * shift
            self.width += 2 * self.merge_layout
        elif self.edges_added > 0 and i == self.mapping[self.width - 2]:
            # columns were added by a merge, but this commit was already in a column: skip over the added ones
            mapping_idx = self.width - 2
            self.edges_added = -1
        else:
            mapping_idx = self.width
            self.width += 2
        self.mapping[mapping_idx] = i

    def _update_columns(self):
        self.columns, self.new_columns = self.new_columns, []
        num_columns = len(self.columns)
        max_new = num_columns + self.num_parents
        self.old_mapping = self.mapping                  # where the previous row left every line (the commit row looks at it to draw a "/")
        self.mapping_size = 2 * max_new
        self.mapping = [-1] * (2 * max_new + 2)
        self.width = 0
        self.prev_edges_added = self.edges_added
        self.edges_added = 0
        seen_this = False
        i = 0
        while i <= num_columns:
            if i == num_columns:
                if seen_this:
                    break
                col_commit = self.commit
            else:
                col_commit = self.columns[i]
            if col_commit == self.commit:
                seen_this = True
                self.commit_index = i
                self.merge_layout = -1
                for parent in self.parents:
                    self._insert_into_new_columns(parent, i)
                if self.num_parents == 0:                  # a root commit still takes up two columns of width
                    self.width += 2
            else:
                self._insert_into_new_columns(col_commit, -1)
            i += 1
        while self.mapping_size > 1 and self.mapping[self.mapping_size - 1] < 0:
            self.mapping_size -= 1

    def update(self, commit, parents):
        """Start the rows of ``commit`` (its parents in order)."""
        self.commit = commit
        self.parents = list(parents)
        self.num_parents = len(self.parents)
        self.prev_commit_index = self.commit_index
        self._update_columns()
        self.expansion_row = 0
        if self.state != PADDING:
            self.state = SKIP
        elif self._needs_pre_commit_line():
            self.state = PRE_COMMIT
        else:
            self.state = COMMIT

    def is_commit_finished(self):
        return self.state == PADDING

    def _is_mapping_correct(self):
        return all(t < 0 or t == i // 2 for i, t in enumerate(self.mapping[:self.mapping_size]))

    # --- the row writers ---------------------------------------------------------------------------------------------------------------------------
    def _padding_line(self):
        self.prev_state = PADDING                         # a plain row after the merge/collapsing rows: they no longer "just happened"
        return "".join("| " for _ in self.new_columns)

    def _skip_line(self):
        self._update_state(PRE_COMMIT if self._needs_pre_commit_line() else COMMIT)
        return "..."

    def _pre_commit_line(self):
        out = []
        seen_this = False
        for i, col in enumerate(self.columns):
            if col == self.commit:
                seen_this = True
                out.append("|" + " " * self.expansion_row)
            elif seen_this and self.expansion_row == 0:
                out.append("\\" if self.prev_state == POST_MERGE and self.prev_commit_index < i else "|")
            elif seen_this and self.expansion_row > 0:
                out.append("\\")
            else:
                out.append("|")
            out.append(" ")
        self.expansion_row += 1
        if not self._needs_pre_commit_line():
            self._update_state(COMMIT)
        return "".join(out)

    def _commit_line(self, mark="*"):
        out = []
        seen_this = False
        num_columns = len(self.columns)
        for i in range(num_columns + 1):
            if i == num_columns:
                if seen_this:
                    break
                col_commit = self.commit
            else:
                col_commit = self.columns[i]
            if col_commit == self.commit:
                seen_this = True
                out.append(mark)
                if self.num_parents > 2 and self.edges_added >= 2:
                    out.append("-" * (2 * self.edges_added - 3) + ".")           # the dashes span the columns this commit adds; a first parent already in a column adds none
            elif seen_this and self.edges_added > 1:
                out.append("\\")
            elif seen_this and self.edges_added == 1:
                # lines right of a merge keep slanting in the very next row, if no plain row came in between (found by comparing with git)
                out.append("\\" if self.prev_state == POST_MERGE and self.prev_edges_added > 0 and self.prev_commit_index < i else "|")
            elif self.prev_state == COLLAPSING and 2 * i + 1 < len(self.old_mapping) and self.old_mapping[2 * i + 1] == i and self.mapping[2 * i] < i:
                out.append("/")
            else:
                out.append("|")
            out.append(" ")
        if self.num_parents > 1:
            self._update_state(POST_MERGE)
        elif self._is_mapping_correct():
            self._update_state(PADDING)
        else:
            self._update_state(COLLAPSING)
        return "".join(out)

    def _post_merge_line(self):
        out = []
        seen_this = False
        first_parent_col = -1
        if self.merge_layout == 0 and self.parents and self.parents[0] in self.columns:
            col = self.columns.index(self.parents[0])                       # where the first parent's line runs now (it moves left in the next row)
            if col < self.commit_index - 1:
                first_parent_col = col
        num_columns = len(self.columns)
        for i in range(num_columns + 1):
            if i == num_columns:
                if seen_this:
                    break
                col_commit = self.commit
            else:
                col_commit = self.columns[i]
            if col_commit == self.commit:
                seen_this = True
                idx = self.merge_layout
                for n, parent in enumerate(self.parents):
                    out.append(MERGE_CHARS[idx])
                    if idx == 2:
                        if self.edges_added > 0 or n < len(self.parents) - 1:
                            out.append(" ")
                    else:
                        idx += 1
                if self.edges_added == 0:
                    out.append(" ")
            elif seen_this:
                out.append("\\" if self.edges_added > 0 else "|")
                out.append(" ")
            else:
                out.append("|")
                if self.merge_layout != 0 or i != self.commit_index - 1:      # with the "/" layout the slash sits right next to the line on its left
                    out.append("_" if first_parent_col >= 0 and first_parent_col < i <= self.commit_index - 2 else " ")        # a first parent far to the left is reached over "_" joins
        self._update_state(PADDING if self._is_mapping_correct() else COLLAPSING)
        return "".join(out)

    def _collapsing_line(self):
        used_horizontal = False
        horizontal_edge = -1
        horizontal_edge_target = -1
        self.old_mapping, self.mapping = self.mapping, [-1] * len(self.mapping)
        for i in range(self.mapping_size):
            target = self.old_mapping[i]
            if target < 0:
                continue
            if target * 2 == i:
                self.mapping[i] = target
            elif self.mapping[i - 1] < 0:
                self.mapping[i - 1] = target
                if horizontal_edge == -1:
                    horizontal_edge = i
                    horizontal_edge_target = target
                    for j in range(target * 2 + 3, i - 2, 2):
                        self.mapping[j] = target
            elif self.mapping[i - 1] == target:
                pass
            else:
                self.mapping[i - 2] = target
                if horizontal_edge == -1:
                    horizontal_edge = i
        if self.mapping[self.mapping_size - 1] < 0:
            self.mapping_size -= 1
        out = []
        for i in range(self.mapping_size):
            target = self.mapping[i]
            if target < 0:
                out.append(" ")
            elif target * 2 == i:
                out.append("|")
            elif target == horizontal_edge_target and i != horizontal_edge - 1:
                if i != target * 2 + 3:
                    self.mapping[i] = -1
                used_horizontal = True
                out.append("_")
            else:
                if used_horizontal and i < horizontal_edge:
                    self.mapping[i] = -1
                out.append("/")
        if self._is_mapping_correct():
            self._update_state(PADDING)
        return "".join(out)

    def next_line(self, mark="*"):
        """The next row of graph text for the current commit, and whether it is the row that carries the commit itself."""
        shown_commit_line = False
        if self.state == PADDING:
            line = self._padding_line()
        elif self.state == SKIP:
            line = self._skip_line()
        elif self.state == PRE_COMMIT:
            line = self._pre_commit_line()
        elif self.state == COMMIT:
            line = self._commit_line(mark)
            shown_commit_line = True
        elif self.state == POST_MERGE:
            line = self._post_merge_line()
        else:
            line = self._collapsing_line()
        return line.ljust(self.width), shown_commit_line

    def remainder(self):
        """The rows still owed once the commit's own text is printed (merge and collapsing rows), as a list."""
        rows = []
        while not self.is_commit_finished():
            rows.append(self.next_line()[0])
        return rows

    def padding_line(self):
        """A row to put in front of a line of text: the next pending graph row, or, before the commit row has been shown, one that leaves every line as it is."""
        if self.state != COMMIT:
            return self.next_line()[0]
        out = []
        for col in self.columns:
            out.append("|")
            out.append(" " * ((self.num_parents - 2) * 2) if col == self.commit and self.num_parents > 2 else " ")
        return "".join(out).ljust(self.width)
