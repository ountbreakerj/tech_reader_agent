"""14 Shell 命令解析的测试。"""
from __future__ import annotations

import unittest

from harness.shell_parse import (
    analyze,
    split_segments,
    to_hook_decision,
)


class SplitTests(unittest.TestCase):
    def test_semicolon_splits(self) -> None:
        parts = split_segments("ls .; cat a.txt")
        self.assertEqual([text for _, text in parts], ["ls .", "cat a.txt"])
        self.assertEqual(parts[1][0], ";")

    def test_quoted_semicolon_does_not_split(self) -> None:
        parts = split_segments('echo "a; b"')
        self.assertEqual([text for _, text in parts], ['echo "a; b"'])

    def test_single_quoted_pipe_does_not_split(self) -> None:
        parts = split_segments("grep 'a|b' file.txt")
        self.assertEqual(len(parts), 1)

    def test_escaped_separator_does_not_split(self) -> None:
        parts = split_segments("echo a\\;b")
        self.assertEqual(len(parts), 1)
        self.assertIn(";", parts[0][1])

    def test_chain_keeps_separator(self) -> None:
        parts = split_segments("ls && cat a")
        self.assertEqual(parts[1][0], "&&")

    def test_pipe_chain_three_segments(self) -> None:
        parts = split_segments("cat a | grep x | wc -l")
        self.assertEqual([sep for sep, _ in parts], ["", "|", "|"])


class AnalyzeTests(unittest.TestCase):
    def test_readonly_pipeline_allowed(self) -> None:
        analysis = analyze("cat a.txt | grep -n todo | wc -l")
        self.assertEqual(analysis.verdict, "readonly")
        self.assertEqual(to_hook_decision(analysis)[0], "allow")

    def test_unknown_program_needs_review(self) -> None:
        analysis = analyze("./deploy.sh --prod")
        self.assertEqual(analysis.verdict, "needs_review")
        self.assertEqual(to_hook_decision(analysis)[0], "ask")

    def test_redirect_write_needs_review(self) -> None:
        analysis = analyze("ls > out.txt")
        self.assertEqual(analysis.verdict, "needs_review")
        self.assertIn("write-redirect", analysis.segments[0].flags)

    def test_command_substitution_is_opaque(self) -> None:
        analysis = analyze("cat $(find . -name secret)")
        self.assertEqual(analysis.verdict, "opaque")
        self.assertEqual(to_hook_decision(analysis)[0], "deny")

    def test_backtick_substitution_is_opaque(self) -> None:
        self.assertEqual(analyze("cat `ls`").verdict, "opaque")

    def test_variable_expansion_is_opaque(self) -> None:
        self.assertEqual(analyze("cat ${TARGET}").verdict, "opaque")

    def test_shell_interpreter_is_opaque(self) -> None:
        self.assertEqual(analyze('sh -c "rm -rf build"').verdict, "opaque")
        self.assertEqual(analyze('bash -lc "ls"').verdict, "opaque")

    def test_python_inline_code_is_opaque(self) -> None:
        self.assertEqual(analyze('python -c "import os; os.remove(\'x\')"').verdict, "opaque")
        # 不带 -c 的 python 跑脚本文件，属于「未知程序」而不是「无法判定」
        self.assertEqual(analyze("python setup.py install").verdict, "needs_review")

    def test_git_write_subcommand_needs_review(self) -> None:
        self.assertEqual(analyze("git status").verdict, "readonly")
        self.assertEqual(analyze("git push origin main").verdict, "needs_review")
        self.assertEqual(analyze("git checkout main").verdict, "needs_review")

    def test_write_flag_needs_review(self) -> None:
        self.assertEqual(analyze("sed -n 1,10p a.txt").verdict, "readonly")
        self.assertEqual(analyze("sed -i s/a/b/ a.txt").verdict, "needs_review")
        self.assertEqual(analyze("sort -o out.txt a.txt").verdict, "needs_review")

    def test_mixed_chain_takes_worst(self) -> None:
        analysis = analyze("ls . && cat a")
        self.assertEqual(analysis.verdict, "readonly")
        self.assertEqual(analyze("ls . && rm -rf build").verdict, "needs_review")
        self.assertEqual(analyze("ls . && sh -c 'x'").verdict, "opaque")

    def test_unclosed_quote_is_opaque(self) -> None:
        analysis = analyze('cat "unterminated')
        self.assertEqual(analysis.verdict, "opaque")
        self.assertTrue(any("无法解析" in reason for reason in analysis.reasons))

    def test_empty_command_is_opaque(self) -> None:
        self.assertEqual(analyze("   ").verdict, "opaque")

    def test_reasons_never_empty(self) -> None:
        self.assertTrue(analyze("ls").reasons)

    def test_programs_are_listed(self) -> None:
        analysis = analyze("ls . && git log -1")
        self.assertEqual(analysis.programs, ["ls", "git"])

    def test_opaque_can_be_downgraded_to_ask(self) -> None:
        analysis = analyze("cat $(ls)")
        self.assertEqual(to_hook_decision(analysis, allow_opaque=True)[0], "ask")


class EdgeCaseTests(unittest.TestCase):
    """正文 hx-14 第 2、4、7 节点名的边界情况。"""

    def test_quoted_redirect_is_literal(self) -> None:
        self.assertEqual(analyze('echo "a > b"').verdict, "readonly")
        self.assertEqual(analyze("grep '>' a.txt").verdict, "readonly")

    def test_harmless_redirects_are_not_writes(self) -> None:
        self.assertEqual(analyze("grep -rn todo . 2>/dev/null").verdict, "readonly")
        self.assertEqual(analyze("ls missing 2>&1 | head").verdict, "readonly")

    def test_redirect_ampersand_does_not_split(self) -> None:
        self.assertEqual(len(split_segments("ls 2>&1")), 1)
        self.assertEqual(len(split_segments("ls &> out.txt")), 1)
        self.assertEqual(analyze("ls &> out.txt").verdict, "needs_review")

    def test_find_delete_and_exec(self) -> None:
        self.assertEqual(analyze('find . -name "*.py"').verdict, "readonly")
        self.assertEqual(analyze('find . -name "*.pyc" -delete').verdict, "needs_review")
        self.assertEqual(analyze("find . -exec rm {} ;").verdict, "opaque")

    def test_read_flags_are_not_write_flags(self) -> None:
        # grep -i 是忽略大小写，grep -o 是只输出匹配部分，都不写文件
        self.assertEqual(analyze("grep -i todo a.txt").verdict, "readonly")
        self.assertEqual(analyze("grep -o x a.txt").verdict, "readonly")

    def test_env_runs_its_argument(self) -> None:
        self.assertEqual(analyze("env rm -rf build").verdict, "needs_review")

    def test_git_branch_with_args(self) -> None:
        self.assertEqual(analyze("git branch").verdict, "readonly")
        self.assertEqual(analyze("git branch -a").verdict, "readonly")
        self.assertEqual(analyze("git branch -D feature").verdict, "needs_review")
        self.assertEqual(analyze("git branch new-feature").verdict, "needs_review")

    def test_awk_script_with_redirect_or_system(self) -> None:
        self.assertEqual(analyze("awk '{print $1}' a.txt").verdict, "readonly")
        self.assertEqual(analyze("awk '{print > \"o.txt\"}' a.txt").verdict, "needs_review")
        self.assertEqual(analyze("awk '{system(\"rm x\")}' a.txt").verdict, "opaque")

    def test_sed_write_command(self) -> None:
        self.assertEqual(analyze("sed -n 'w out.txt' a.txt").verdict, "needs_review")

    def test_single_quoted_substitution_is_literal(self) -> None:
        self.assertEqual(analyze("echo '$(whoami)'").verdict, "readonly")
        self.assertEqual(analyze('echo "$(whoami)"').verdict, "opaque")

    def test_plain_variable_is_opaque(self) -> None:
        self.assertEqual(analyze("cat $FILE").verdict, "opaque")

    def test_uniq_output_file(self) -> None:
        self.assertEqual(analyze("uniq a.txt").verdict, "readonly")
        self.assertEqual(analyze("uniq a.txt b.txt").verdict, "needs_review")

    def test_opaque_reason_tells_model_what_to_do(self) -> None:
        _, reason = to_hook_decision(analyze("cat $(ls)"))
        self.assertIn("直接写出来", reason)


if __name__ == "__main__":
    unittest.main()
