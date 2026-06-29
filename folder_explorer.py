"""File-explorer-style GUI for browsing folder/file metadata.

    python folder_explorer.py [start_path]
"""
import os
import sys
import threading
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from tkinter import filedialog, messagebox, ttk

from folder_meta import _created, _dt, human_size, scan_parallel, write_growth_xlsx, write_xlsx

COLS = ("size", "items", "type", "created", "modified")


def recursive_size(path: str) -> tuple[int, int]:
    """(total_bytes, total_files) under path. Best-effort: unreadable entries skipped."""
    total = count = 0
    for dirpath, _dirs, files in os.walk(path):
        for fn in files:
            try:
                total += os.stat(os.path.join(dirpath, fn)).st_size
                count += 1
            except OSError:
                pass
    return total, count


class Explorer:
    def __init__(self, start: str):
        self.paths: dict[str, str] = {}        # tree-item id -> filesystem path
        self.populated: set[str] = set()
        self.children_order: dict[str, list[str]] = {}  # parent iid -> canonical child order
        self.meta: dict[str, dict] = {}        # iid -> raw values for sort/filter (display text lies)
        self.sort_col = "name"
        self.sort_desc = False
        self.pool = ThreadPoolExecutor(max_workers=8)  # bound threads; size jobs are I/O-bound

        self.win = tk.Tk()
        self.win.title("Folder Metadata Explorer")
        self.win.geometry("1100x650")

        bar = ttk.Frame(self.win)
        bar.pack(fill="x", padx=6, pady=6)
        self.path_var = tk.StringVar(value=start)
        ttk.Entry(bar, textvariable=self.path_var).pack(side="left", fill="x", expand=True)
        ttk.Button(bar, text="Open", command=self.open_path).pack(side="left", padx=3)
        ttk.Button(bar, text="Add", command=self.add_folder).pack(side="left")
        ttk.Button(bar, text="Browse…", command=self.browse).pack(side="left", padx=3)
        ttk.Button(bar, text="Export Summary", command=lambda: self.export(summary=True)).pack(side="left", padx=3)
        ttk.Button(bar, text="Export Detail", command=lambda: self.export(summary=False)).pack(side="left")

        fbar = ttk.Frame(self.win)
        fbar.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Label(fbar, text="Filter:").pack(side="left")
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self.apply_filter())  # live filtering
        ttk.Entry(fbar, textvariable=self.filter_var).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(fbar, text="Clear", command=lambda: self.filter_var.set("")).pack(side="left")

        self.tree = ttk.Treeview(self.win, columns=COLS)
        self.tree.heading("#0", text="Name", command=lambda: self.sort_by("name"))
        self.tree.column("#0", width=420, anchor="w")
        widths = {"size": 110, "items": 80, "type": 90, "created": 160, "modified": 160}
        for c in COLS:
            self.tree.heading(c, text=c.capitalize(), command=lambda c=c: self.sort_by(c))
            self.tree.column(c, width=widths[c], anchor="e" if c in ("size", "items") else "w")
        vsb = ttk.Scrollbar(self.win, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.tree.bind("<<TreeviewOpen>>", self.on_open)

        self.status = ttk.Label(self.win, text="", anchor="w", relief="sunken")
        self.status.pack(fill="x", side="bottom")

        self.open_path()

    # ---- tree population -------------------------------------------------
    def open_path(self):
        """Replace the whole session with a single root."""
        self.tree.delete(*self.tree.get_children())
        self.paths.clear()
        self.populated.clear()
        self.children_order.clear()
        self.meta.clear()
        self._add_root(self.path_var.get())

    def add_folder(self):
        """Append another root to the current session, keeping existing ones."""
        self._add_root(self.path_var.get())

    def _add_root(self, raw: str):
        path = raw.strip().strip('"')
        if not os.path.isdir(path):
            messagebox.showerror("Not found", f"Not a folder:\n{path}")
            return
        if any(self.paths.get(r) == path for r in self.children_order.get("", [])):
            return  # already a root in this session
        root = self._add_node("", path, path)  # full path as label so multiple roots stay distinct
        self.meta[root] = {"name": path, "is_dir": True, "_size": -1, "_items": 0}
        self.tree.set(root, "size", "…")
        self.tree.set(root, "type", "folder")
        self._queue_size(root, path)
        self.tree.item(root, open=True)
        self._populate(root)

    def _add_node(self, parent: str, path: str, label: str) -> str:
        iid = self.tree.insert(parent, "end", text=label)
        self.paths[iid] = path
        self.children_order.setdefault(parent, []).append(iid)
        return iid

    def on_open(self, _event):
        node = self.tree.focus()
        if node and node not in self.populated:
            self._populate(node)

    def _populate(self, node: str):
        self.populated.add(node)
        self.tree.delete(*self.tree.get_children(node))
        path = self.paths[node]
        try:
            entries = sorted(os.scandir(path), key=lambda e: (e.is_file(), e.name.lower()))
        except OSError as e:
            self._add_node(node, path, f"⚠ {e.strerror}")
            return
        for e in entries:
            try:
                st = e.stat(follow_symlinks=False)
                created, modified = _created(st), _dt(st.st_mtime)
                is_dir = e.is_dir(follow_symlinks=False)
                iid = self._add_node(node, e.path, e.name)
                if is_dir:
                    self.tree.set(iid, "size", "…")
                    self.tree.set(iid, "type", "folder")
                    self.tree.insert(iid, "end", text="loading…")  # placeholder → expandable arrow
                    self._queue_size(iid, e.path)
                    size, ftype = -1, "folder"
                else:
                    size = st.st_size
                    ftype = os.path.splitext(e.name)[1].lower() or "(no ext)"
                    self.tree.set(iid, "size", human_size(size))
                    self.tree.set(iid, "type", ftype)
                self.tree.set(iid, "created", created)
                self.tree.set(iid, "modified", modified)
                self.meta[iid] = {"name": e.name, "is_dir": is_dir, "type": ftype,
                                  "created": created, "modified": modified, "_size": size, "_items": 0}
            except OSError:
                self._add_node(node, e.path, f"⚠ {e.name}")

        if self.sort_col != "name" or self.sort_desc:
            self._sort_node(node)
        if self.filter_var.get().strip():
            self.apply_filter()

    def _queue_size(self, iid: str, path: str):
        def work():
            size, count = recursive_size(path)
            self.win.after(0, lambda: self._set_size(iid, size, count))
        self.pool.submit(work)

    def _set_size(self, iid: str, size: int, count: int):
        if self.tree.exists(iid):
            self.tree.set(iid, "size", human_size(size))
            self.tree.set(iid, "items", str(count))
        if iid in self.meta:
            self.meta[iid]["_size"] = size       # click header to re-sort by size
            self.meta[iid]["_items"] = count

    # ---- sort and filter ---------------------------------------------------
    def sort_by(self, col: str):
        self.sort_desc = (not self.sort_desc) if self.sort_col == col else False
        self.sort_col = col
        self._sort_node("")
        arrow = " ▼" if self.sort_desc else " ▲"
        self.tree.heading("#0", text="Name" + (arrow if col == "name" else ""))
        for c in COLS:
            self.tree.heading(c, text=c.capitalize() + (arrow if col == c else ""))
        if self.filter_var.get().strip():
            self.apply_filter()

    def _sort_node(self, parent: str):
        kids = self.children_order.get(parent)
        if not kids:
            return
        col = self.sort_col

        def key(i):
            m = self.meta.get(i, {})
            if col in ("size", "items"):
                return m.get("_" + col, -1)
            if col == "name":
                return m.get("name", "").lower()
            return str(m.get(col, ""))

        kids = sorted(kids, key=key, reverse=self.sort_desc)
        kids.sort(key=lambda i: not self.meta.get(i, {}).get("is_dir", False))  # folders first (stable)
        self.children_order[parent] = kids
        for idx, k in enumerate(kids):
            if self.tree.exists(k):
                self.tree.move(k, parent, idx)
            self._sort_node(k)

    def apply_filter(self):
        text = self.filter_var.get().strip().lower()
        if not text:  # restore everything in canonical (sorted) order
            for parent, kids in self.children_order.items():
                for idx, k in enumerate(kids):
                    if self.tree.exists(k):
                        self.tree.move(k, parent, idx)
            return

        # only filters loaded levels — collapsed folders aren't scanned until expanded
        visible: set[str] = set()

        def visit(iid) -> bool:
            match = text in self.meta.get(iid, {}).get("name", "").lower()
            child_match = any(visit(c) for c in self.children_order.get(iid, []))
            if match or child_match:
                visible.add(iid)
            return match or child_match

        for root in self.children_order.get("", []):
            visit(root)

        def apply(parent):
            idx = 0
            for k in self.children_order.get(parent, []):
                if k in visible:
                    self.tree.move(k, parent, idx)
                    self.tree.item(k, open=True)
                    idx += 1
                    apply(k)
                else:
                    self.tree.detach(k)

        apply("")

    # ---- buttons ---------------------------------------------------------
    def browse(self):
        d = filedialog.askdirectory(initialdir=self.path_var.get() or ".")
        if d:
            self.path_var.set(d)
            self.open_path()

    def export(self, summary=True):
        roots = [self.paths[r] for r in self.children_order.get("", [])]
        if not roots:
            self.status.config(text="Nothing loaded to export.")
            return
        default = "storage_summary.xlsx" if summary else "folder_metadata.xlsx"
        path = filedialog.asksaveasfilename(
            title="Export to Excel", initialfile=default,
            defaultextension=".xlsx", filetypes=[("Excel workbook", "*.xlsx")])
        if not path:
            return
        if not path.lower().endswith(".xlsx"):
            path += ".xlsx"
        self.status.config(text=f"Exporting {len(roots)} folder(s) ... scanning")

        def report(msg):  # push progress text to the status bar from the worker thread
            self.win.after(0, lambda: self.status.config(text=msg))

        def work():
            try:
                folders, files = [], []
                for ri, r in enumerate(roots, 1):
                    prog = lambda d, t, ri=ri, r=r: report(
                        f"[{ri}/{len(roots)}] scanning {r}: {d}/{t} top-level folders done")
                    f_rows, file_rows = scan_parallel(r, progress=prog)
                    folders += f_rows
                    files += file_rows
                report(f"Writing workbook ({len(folders)} folders, {len(files)} files) ...")
                folders.sort(key=lambda x: x.get("folder", "").lower())
                files.sort(key=lambda x: x.get("path", "").lower())
                if summary:
                    write_growth_xlsx(path, folders, files)
                else:
                    write_xlsx(path, folders, files)
                done = (len(folders), len(files), summary, None)
            except Exception as e:
                done = (0, 0, summary, f"{type(e).__name__}: {e}")
            self.win.after(0, lambda: self._export_done(path, *done))

        threading.Thread(target=work, daemon=True).start()

    def _export_done(self, path, n_folders, n_files, summary, error):
        if error:
            self.status.config(text=f"Export failed: {error}")
            messagebox.showerror("Export failed", error)
        else:
            kind = "executive summary" if summary else "full detail (Tree + Files)"
            msg = f"Exported {kind} from {n_folders} folders / {n_files} files to:\n{path}"
            self.status.config(text=msg.replace(chr(10), "  "))
            messagebox.showinfo("Export complete", msg)

    def run(self):
        self.win.mainloop()


if __name__ == "__main__":
    start = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    Explorer(start).run()


def test_human_size():
    assert human_size(0) == "0 B"
    assert human_size(1023) == "1023 B"
    assert human_size(1024) == "1.0 KB"
    assert human_size(1024 ** 2) == "1.0 MB"
    assert human_size(int(1.5 * 1024 ** 3)) == "1.5 GB"
    print("test_human_size ok")
