"""Compare compiled code and actual Python docstring locations. Never import patched code."""
import ast
import sys
import types


def code_shape(code):
    constants = tuple(code_shape(value) if isinstance(value, types.CodeType)
                      else (type(value), repr(value)) for value in code.co_consts)
    return (code.co_code, constants, code.co_names, code.co_varnames, code.co_freevars,
            code.co_cellvars, code.co_flags, code.co_argcount, code.co_posonlyargcount,
            code.co_kwonlyargcount, code.co_exceptiontable, code.co_qualname)


def without_docstrings(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if ast.get_docstring(node, clean=False) is not None:
                node.body[0].value.value = ""
    return ast.dump(tree)


def compare(old, new):
    # Both must compile in the scorer's CPython even if their parse trees agree.
    if code_shape(compile(old, "<change>", "exec", dont_inherit=True)) == code_shape(
            compile(new, "<change>", "exec", dont_inherit=True)):
        return "code"
    if without_docstrings(ast.parse(old)) == without_docstrings(ast.parse(new)):
        return "docs"
    return "different"


if __name__ == "__main__":
    try:
        old, new = sys.stdin.buffer.read().split(b"\0")
        print(compare(old.decode(), new.decode()))
    except (SyntaxError, UnicodeError, ValueError, RecursionError, OverflowError):
        print("different")
