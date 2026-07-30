#!/usr/bin/env python3
"""Study Agent — デスクトップGUI (Tkinter、追加依存なし)。

初回起動時にセットアップ画面(使うAPIの選択・キー入力・初期クレジット)を表示。
以降は小さなウィンドウから notes / draft / browse を実行できる。
"""
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from study_agent import settings as st
from study_agent.llm import PROVIDERS


# ---------------------------------------------------------------- setup wizard
class SetupWizard(tk.Toplevel):
    def __init__(self, master, on_done):
        super().__init__(master)
        self.title("初回セットアップ")
        self.on_done = on_done
        self.resizable(False, False)
        s = st.load()

        frm = ttk.Frame(self, padding=16)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="ようこそ 👋 使うAPIを選んでキーを入力してください",
                  font=("", 11, "bold")).grid(column=0, row=0, columnspan=2,
                                              pady=(0, 10), sticky="w")

        ttk.Label(frm, text="メインで使うAPI:").grid(column=0, row=1, sticky="w")
        self.default_var = tk.StringVar(value=s["default_provider"])
        ttk.Combobox(frm, textvariable=self.default_var, state="readonly",
                     values=list(PROVIDERS), width=12).grid(column=1, row=1,
                                                            sticky="w")

        self.key_vars = {}
        for i, name in enumerate(PROVIDERS, start=2):
            ttk.Label(frm, text=f"{name} APIキー:").grid(column=0, row=i,
                                                         sticky="w", pady=2)
            var = tk.StringVar(value=s["api_keys"].get(name, ""))
            ttk.Entry(frm, textvariable=var, width=38, show="*").grid(
                column=1, row=i, pady=2)
            self.key_vars[name] = var

        row = 2 + len(PROVIDERS)
        ttk.Label(frm, text="初期クレジット (USD):").grid(column=0, row=row,
                                                          sticky="w", pady=2)
        self.credit_var = tk.StringVar(value=str(s["credits_remaining"]))
        ttk.Entry(frm, textvariable=self.credit_var, width=10).grid(
            column=1, row=row, sticky="w")

        ttk.Label(frm, foreground="gray",
                  text="※ 未使用のAPIのキーは空欄でOK。あとで設定から変更できます。"
                 ).grid(column=0, row=row + 1, columnspan=2, sticky="w",
                        pady=(8, 4))

        ttk.Button(frm, text="保存して開始", command=self._save).grid(
            column=1, row=row + 2, sticky="e", pady=(8, 0))

    def _save(self):
        s = st.load()
        s["default_provider"] = self.default_var.get()
        for name, var in self.key_vars.items():
            s["api_keys"][name] = var.get().strip()
        try:
            s["credits_remaining"] = float(self.credit_var.get())
        except ValueError:
            messagebox.showerror("エラー", "クレジットは数値で入力してください")
            return
        if not s["api_keys"][s["default_provider"]]:
            messagebox.showerror("エラー",
                                 f"{s['default_provider']} のキーが空です")
            return
        s["setup_done"] = True
        st.save(s)
        self.destroy()
        self.on_done()


# -------------------------------------------------------------- memory panel
class MemoryManager(tk.Toplevel):
    """学期・科目方針・ログイン情報の長期メモリ管理。"""

    def __init__(self, master):
        super().__init__(master)
        from study_agent import memory as mem
        self.mem = mem
        self.title("長期メモリ")
        self.geometry("520x520")
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        nb.add(self._term_tab(), text="学期")
        nb.add(self._course_tab(), text="科目・課題方針")
        nb.add(self._cred_tab(), text="ログイン情報")

    def _labeled(self, parent, label, r, show=None):
        ttk.Label(parent, text=label).grid(column=0, row=r, sticky="w", pady=2)
        v = tk.StringVar()
        ttk.Entry(parent, textvariable=v, width=34, show=show or "").grid(
            column=1, row=r, pady=2)
        return v

    def _term_tab(self):
        f = ttk.Frame(self, padding=12)
        name = self._labeled(f, "学期名 (例 Fall 2026)", 0)
        start = self._labeled(f, "開始 YYYY-MM-DD", 1)
        end = self._labeled(f, "終了 YYYY-MM-DD", 2)
        out = tk.Text(f, height=8, width=52, state="disabled")
        out.grid(column=0, row=4, columnspan=2, pady=(8, 0))

        def refresh():
            out.config(state="normal"); out.delete("1.0", "end")
            for t in self.mem.list_terms():
                out.insert("end", f"{t['name']}: {t['start']} 〜 {t['end']}\n")
            out.config(state="disabled")

        def save():
            try:
                self.mem.add_term(name.get(), start.get(), end.get())
                refresh()
            except Exception as e:  # noqa: BLE001
                messagebox.showerror("エラー", str(e))
        ttk.Button(f, text="保存", command=save).grid(column=1, row=3,
                                                       sticky="e", pady=4)
        refresh()
        return f

    def _course_tab(self):
        f = ttk.Frame(self, padding=12)
        name = self._labeled(f, "科目名 (例 MGMT 109)", 0)
        term = self._labeled(f, "所属学期", 1)
        sites = self._labeled(f, "使うサイト (カンマ区切り)", 2)
        ttk.Label(f, text="課題の出され方(初日に確認):").grid(
            column=0, row=3, columnspan=2, sticky="w", pady=(6, 0))
        policy = tk.Text(f, height=4, width=52)
        policy.grid(column=0, row=4, columnspan=2)

        def save():
            site_list = [s.strip() for s in sites.get().split(",") if s.strip()]
            self.mem.set_course(name.get(), term.get(),
                                policy.get("1.0", "end").strip(), site_list)
            messagebox.showinfo("保存", f"{name.get()} を保存しました")
        ttk.Button(f, text="保存", command=save).grid(column=1, row=5,
                                                       sticky="e", pady=6)
        return f

    def _cred_tab(self):
        f = ttk.Frame(self, padding=12)
        ttk.Label(f, foreground="gray", wraplength=460,
                  text="パスワードはOSキーチェーンに保存され、平文ファイルには"
                       "書き込みません。ログイン時もAIには渡りません。").grid(
            column=0, row=0, columnspan=2, sticky="w", pady=(0, 8))
        site = self._labeled(f, "サイト (例 canvas.eee.uci.edu)", 1)
        user = self._labeled(f, "ユーザー名", 2)
        pw = self._labeled(f, "パスワード", 3, show="*")
        status = ttk.Label(f, foreground="gray")
        status.grid(column=0, row=5, columnspan=2, sticky="w", pady=(6, 0))

        def save():
            if not site.get() or not pw.get():
                messagebox.showerror("エラー", "サイトとパスワードは必須です")
                return
            backend = self.mem.set_credential(site.get(), user.get(), pw.get())
            pw.set("")
            status.config(text=f"保存済みサイト: "
                               f"{', '.join(self.mem.list_credential_sites())} "
                               f"(backend: {backend})")
        ttk.Button(f, text="保存", command=save).grid(column=1, row=4,
                                                       sticky="e", pady=4)
        status.config(text=f"保存済み: "
                           f"{', '.join(self.mem.list_credential_sites()) or 'なし'}")
        return f


# ------------------------------------------------------------------ main app
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Study Agent")
        self.geometry("460x560")
        self.attributes("-topmost", True)  # デスクトップに常駐する小窓

        top = ttk.Frame(self, padding=(12, 10, 12, 0))
        top.pack(fill="x")
        self.credit_label = ttk.Label(top, font=("", 10, "bold"))
        self.credit_label.pack(side="left")
        ttk.Button(top, text="+ チャージ", width=9,
                   command=self.charge).pack(side="right")
        ttk.Button(top, text="🧠 メモリ", width=8,
                   command=self.open_memory).pack(side="right", padx=4)
        ttk.Button(top, text="⚙ 設定", width=7,
                   command=self.open_setup).pack(side="right", padx=4)

        prov = ttk.Frame(self, padding=(12, 6))
        prov.pack(fill="x")
        ttk.Label(prov, text="プロバイダ:").pack(side="left")
        self.provider_var = tk.StringVar(value="auto")
        ttk.Combobox(prov, textvariable=self.provider_var, state="readonly",
                     values=["auto"] + list(PROVIDERS), width=10
                     ).pack(side="left", padx=6)
        ttk.Label(prov, text="(auto = 内容で自動選択)",
                  foreground="gray").pack(side="left")

        btns = ttk.Frame(self, padding=(12, 4))
        btns.pack(fill="x")
        ttk.Button(btns, text="📝 ノート作成",
                   command=lambda: self.pick_and_run("notes")).pack(
            side="left", expand=True, fill="x", padx=(0, 4))
        ttk.Button(btns, text="✍️ 課題ドラフト",
                   command=lambda: self.pick_and_run("draft")).pack(
            side="left", expand=True, fill="x", padx=(4, 0))

        brw = ttk.Frame(self, padding=(12, 6))
        brw.pack(fill="x")
        self.task_var = tk.StringVar()
        ttk.Entry(brw, textvariable=self.task_var).pack(
            side="left", expand=True, fill="x")
        ttk.Button(brw, text="🌐 実行",
                   command=self.run_browse).pack(side="left", padx=(6, 0))

        self.log = tk.Text(self, height=18, wrap="word", state="disabled",
                           font=("", 10))
        self.log.pack(fill="both", expand=True, padx=12, pady=(4, 12))

        self.refresh_credits()
        if st.first_run_needed():
            self.after(200, self.open_setup)

    # ---- helpers
    def open_setup(self):
        SetupWizard(self, on_done=self.refresh_credits)

    def refresh_credits(self):
        s = st.load()
        self.credit_label.config(
            text=f"💳 残高: ${s['credits_remaining']:.2f}")

    def open_memory(self):
        MemoryManager(self)

    def charge(self):
        win = tk.Toplevel(self)
        win.title("チャージ")
        ttk.Label(win, text="追加額 (USD):", padding=10).pack(side="left")
        var = tk.StringVar(value="5")
        ttk.Entry(win, textvariable=var, width=8).pack(side="left")

        def do():
            try:
                st.add_credits(float(var.get()))
                self.refresh_credits()
                win.destroy()
            except ValueError:
                messagebox.showerror("エラー", "数値を入力してください")
        ttk.Button(win, text="OK", command=do).pack(side="left", padx=10)

    def println(self, text):
        self.log.config(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def _run_bg(self, fn):
        def wrapper():
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                self.after(0, self.println, f"❌ エラー: {e}")
            self.after(0, self.refresh_credits)
        threading.Thread(target=wrapper, daemon=True).start()

    # ---- actions
    def pick_and_run(self, kind):
        path = filedialog.askopenfilename(
            filetypes=[("教材/課題", "*.pdf *.docx *.pptx *.txt *.md")])
        if not path:
            return
        provider = self.provider_var.get()
        label = "ノート" if kind == "notes" else "ドラフト"
        self.println(f"▶ {label}作成: {path}")

        def job():
            if kind == "notes":
                from study_agent.notes import make_notes
                out = make_notes(path, provider=provider)
            else:
                from study_agent.assignment import make_draft
                out = make_draft(path, provider=provider)
                self.after(0, self.println,
                           "   ※ DRAFTです。確認・修正してから使ってください。")
            self.after(0, self.println, f"✅ 保存: {out}")
        self._run_bg(job)

    def run_browse(self):
        task = self.task_var.get().strip()
        if not task:
            return
        self.println(f"▶ ブラウザタスク: {task}")

        def job():
            from study_agent.browser import browse
            result = browse(task)
            self.after(0, self.println, f"✅ 結果:\n{result}")
        self._run_bg(job)


if __name__ == "__main__":
    App().mainloop()
