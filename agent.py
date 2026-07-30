#!/usr/bin/env python3
"""Study Agent — ノート作成 / 課題ドラフト / ブラウザ操作 CLI。

例:
  python agent.py notes textbook_ch3.pdf
  python agent.py notes slides.pptx -o ch3_notes.docx --style summary
  python agent.py draft practice_set2.pdf
  python agent.py draft hw.docx --extra "Pythonで解くこと"
  python agent.py browse "CanvasのMGMT109のToDoを一覧して教えて"
"""
import argparse
import sys


def main():
    parser = argparse.ArgumentParser(description="Study Agent")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_notes = sub.add_parser("notes", help="教材からノート(.docx)を作成")
    p_notes.add_argument("input", help="pdf/docx/pptx/txt/md ファイル")
    p_notes.add_argument("-o", "--out", help="出力先 .docx")
    p_notes.add_argument("--lang", default="日本語", help="出力言語 (例: English)")
    p_notes.add_argument("--style", choices=["detailed", "summary"],
                         default="detailed")
    p_notes.add_argument("--provider", default="auto",
                         choices=["auto", "claude", "openai", "deepseek"])

    p_draft = sub.add_parser("draft", help="練習課題のドラフト+解説を作成")
    p_draft.add_argument("input", help="課題ファイル (pdf/docx/txt/md)")
    p_draft.add_argument("-o", "--out", help="出力先 .docx")
    p_draft.add_argument("--lang", default="日本語")
    p_draft.add_argument("--extra", default="", help="追加指示")
    p_draft.add_argument("--provider", default="auto",
                         choices=["auto", "claude", "openai", "deepseek"])

    p_browse = sub.add_parser("browse", help="ブラウザタスクを実行 (browser-use)")
    p_browse.add_argument("task", help="自然言語のタスク")
    p_browse.add_argument("--config", default="config.yaml")
    p_browse.add_argument("--course", help="科目文脈を差し込む (例: 'MGMT 109')")

    p_quiz = sub.add_parser("quiz", help="その日のノートを根拠にクイズへ回答")
    p_quiz.add_argument("input", help="クイズ問題ファイル")
    p_quiz.add_argument("--notes-dir", default=".", help="ノートの保存フォルダ")
    p_quiz.add_argument("--course")
    p_quiz.add_argument("--provider", default="auto",
                        choices=["auto", "claude", "openai", "deepseek"])

    p_term = sub.add_parser("term", help="学期(クォーター/セメスター)を登録")
    p_term.add_argument("name")
    p_term.add_argument("start", help="YYYY-MM-DD")
    p_term.add_argument("end", help="YYYY-MM-DD")

    p_course = sub.add_parser("course", help="科目の課題方針(初日に確認)を登録")
    p_course.add_argument("name")
    p_course.add_argument("term")
    p_course.add_argument("policy", help="課題の出され方の説明")
    p_course.add_argument("--sites", nargs="*", default=[])

    p_cred = sub.add_parser("cred", help="サイトのログイン情報を保存(キーチェーン)")
    p_cred.add_argument("site")
    p_cred.add_argument("username")

    p_clean = sub.add_parser("cleanup", help="終了した学期の古いメモリを削除")
    p_clean.add_argument("--grace-days", type=int, default=30)
    p_clean.add_argument("--delete-credentials", action="store_true")

    args = parser.parse_args()

    if args.cmd == "notes":
        from study_agent.notes import make_notes
        out = make_notes(args.input, args.out, lang=args.lang,
                         style=args.style, provider=args.provider)
        print(f"✅ ノートを保存しました: {out}")

    elif args.cmd == "draft":
        from study_agent.assignment import make_draft
        out = make_draft(args.input, args.out, lang=args.lang,
                         extra_instructions=args.extra,
                         provider=args.provider)
        print(f"✅ ドラフトを保存しました: {out}")
        print("   ※ 内容を確認・修正してから使ってください。")

    elif args.cmd == "browse":
        from study_agent.browser import browse
        result = browse(args.task, config_path=args.config, course=args.course)
        print("\n=== 結果 ===")
        print(result)

    elif args.cmd == "quiz":
        from study_agent.quiz import answer_quiz
        print(answer_quiz(args.input, notes_dir=args.notes_dir,
                          course=args.course, provider=args.provider))

    elif args.cmd == "term":
        from study_agent import memory as mem
        mem.add_term(args.name, args.start, args.end)
        print(f"✅ 学期を登録: {args.name} ({args.start}〜{args.end})")

    elif args.cmd == "course":
        from study_agent import memory as mem
        mem.set_course(args.name, args.term, args.policy, args.sites)
        print(f"✅ 科目を登録: {args.name}")

    elif args.cmd == "cred":
        import getpass
        from study_agent import memory as mem
        pw = getpass.getpass(f"{args.site} のパスワード(表示されません): ")
        backend = mem.set_credential(args.site, args.username, pw)
        print(f"✅ 保存しました (backend: {backend})")

    elif args.cmd == "cleanup":
        from study_agent import memory as mem
        removed = mem.cleanup_expired(grace_days=args.grace_days,
                                      delete_credentials=args.delete_credentials)
        print(f"削除した科目メモリ: {removed or '(なし)'}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
