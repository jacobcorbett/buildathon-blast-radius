#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#include <tree_sitter/api.h>

const TSLanguage *tree_sitter_python(void);

static bool take_leaf(TSTreeCursor *cursor, bool *started, TSNode *leaf) {
    if (*started) {
        while (!ts_tree_cursor_goto_next_sibling(cursor)) {
            if (!ts_tree_cursor_goto_parent(cursor)) {
                return false;
            }
        }
    }
    *started = true;
    while (ts_tree_cursor_goto_first_child(cursor)) {
    }
    *leaf = ts_tree_cursor_current_node(cursor);
    return true;
}

static bool take_code_leaf(TSTreeCursor *cursor, bool *started, TSNode *leaf) {
    while (take_leaf(cursor, started, leaf)) {
        if (strcmp(ts_node_type(*leaf), "comment") != 0) {
            return true;
        }
    }
    return false;
}

static bool same_code(const TSTree *before, const char *old,
                      const TSTree *after, const char *updated) {
    TSTreeCursor left = ts_tree_cursor_new(ts_tree_root_node(before));
    TSTreeCursor right = ts_tree_cursor_new(ts_tree_root_node(after));
    TSNode a, b;
    bool left_started = false, right_started = false;
    bool equal;
    while (true) {
        bool old_leaf = take_code_leaf(&left, &left_started, &a);
        bool new_leaf = take_code_leaf(&right, &right_started, &b);
        if (!old_leaf || !new_leaf) {
            equal = old_leaf == new_leaf;
            break;
        }
        uint32_t a_start = ts_node_start_byte(a);
        uint32_t b_start = ts_node_start_byte(b);
        uint32_t a_size = ts_node_end_byte(a) - a_start;
        uint32_t b_size = ts_node_end_byte(b) - b_start;
        if (a_size != b_size || ts_node_symbol(a) != ts_node_symbol(b)
            || ts_node_start_point(a).column != ts_node_start_point(b).column
            || memcmp(old + a_start, updated + b_start, a_size) != 0) {
            equal = false;
            break;
        }
    }
    ts_tree_cursor_delete(&left);
    ts_tree_cursor_delete(&right);
    return equal;
}

// 0 means uncertain syntax, 1 means valid syntax, 2 means identical code tokens.
int python_syntax(const char *old, uint32_t old_len, const char *updated,
                  uint32_t new_len, bool compare_comments) {
    int result = 0;
    TSParser *parser = ts_parser_new();
    if (!ts_parser_set_language(parser, tree_sitter_python())) {
        ts_parser_delete(parser);
        return 0;
    }
    TSTree *after = ts_parser_parse_string(parser, NULL, updated, new_len);
    if (after && !ts_node_has_error(ts_tree_root_node(after))) {
        result = 1;
        if (compare_comments) {
            TSTree *before = ts_parser_parse_string(parser, NULL, old, old_len);
            if (before && !ts_node_has_error(ts_tree_root_node(before))
                && same_code(before, old, after, updated)) {
                result = 2;
            }
            ts_tree_delete(before);
        }
    }
    ts_tree_delete(after);
    ts_parser_delete(parser);
    return result;
}
