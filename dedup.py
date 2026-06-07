"""
Mailing List Deduplication Tool  —  Fox Cities PAC / Ticket Services
======================================================================
Mode A: Deduplicate only
Mode B: Deduplicate + verify addresses via Smarty US Street API

Smarty API docs: https://www.smarty.com/docs/cloud/us-street-api
Get a free key:  https://www.smarty.com/pricing

Outputs
-------
  <name>_deduped_YYYYMMDD.csv          — clean list ready for mail house
  <name>_flagged_YYYYMMDD.csv          — records Smarty could not verify
                                         (feed back into Archtics for cleanup)

Install deps:
  pip install smartystreets-python-sdk
"""

import csv
import os
import re
import threading
import tkinter as tk
from tkinter import filedialog, scrolledtext
from datetime import datetime
from pathlib import Path

import tkinter.ttk as ttk
from tkinter.constants import *


# ── Address normalisation ─────────────────────────────────────────────────────

STREET_ABBREVS = {
    r'\bROAD\b':        'RD',
    r'\bSTREET\b':      'ST',
    r'\bAVENUE\b':      'AVE',
    r'\bDRIVE\b':       'DR',
    r'\bLANE\b':        'LN',
    r'\bCOURT\b':       'CT',
    r'\bBOULEVARD\b':   'BLVD',
    r'\bCIRCLE\b':      'CIR',
    r'\bPLACE\b':       'PL',
    r'\bTRAIL\b':       'TRL',
    r'\bHEIGHTS\b':     'HTS',
    r'\bCOUNTY ROAD\b': 'CTY RD',
    r'\bCTY\.?\s*RD\b': 'CTY RD',
    r'\bCO\.?\s*RD\b':  'CTY RD',
    r'\bNORTH\b':       'N',
    r'\bSOUTH\b':       'S',
    r'\bEAST\b':        'E',
    r'\bWEST\b':        'W',
}

def normalize_address(addr):
    if not addr:
        return ''
    s = addr.upper().strip()
    s = re.sub(r'\.', '', s)
    s = re.sub(r'\s+', ' ', s)
    for pattern, replacement in STREET_ABBREVS.items():
        s = re.sub(pattern, replacement, s)
    s = re.sub(r'[^\w\s#\-/]', '', s)
    return s.strip()

def normalize_zip(z):
    if not z:
        return ''
    return re.sub(r'\D', '', z.strip())[:5]

def make_dedup_key(row):
    addr  = normalize_address(row.get('street_addr_1', ''))
    city  = (row.get('city', '') or '').upper().strip()
    zip5  = normalize_zip(row.get('zip', ''))
    addr2 = row.get('street_addr_2', '') or ''
    if re.search(r'\bPO\b|\bBOX\b|\bPOST\b', addr):   # addr is already uppercase; periods stripped by normalize_address
        return f"{addr}|{normalize_address(addr2)}|{city}|{zip5}"
    return f"{addr}|{city}|{zip5}"

# Field names that indicate a company/organisation record — checked in order;
# update this tuple if your Archtics export uses a different column name.
_COMPANY_FIELDS = ('company_name',)

def _has_company(row):
    return any(row.get(f, '').strip() for f in _COMPANY_FIELDS)

def completeness_score(row):
    score = sum(1 for v in row.values() if v and str(v).strip())
    for field in ('email_addr', 'phone_day', 'name_first', 'name'):
        if row.get(field, '').strip():
            score += 2
    if _has_company(row):
        score += 3
    return score

def _dedup_sort_key(r):
    try:    acct = int(r.get('acct_id', 0) or 0)
    except: acct = 999_999_999
    # company record first, then most complete, then lowest acct_id
    return (0 if _has_company(r) else 1, -completeness_score(r), acct)


# ── Smarty verification ───────────────────────────────────────────────────────

# dpv_match_code meanings (USPS Delivery Point Validation)
DPV_LABELS = {
    'Y':  'Confirmed',          # full match
    'S':  'Confirmed (no unit)',# primary matches, secondary missing/invalid
    'D':  'Confirmed (no unit needed)',
    'N':  'Not confirmed',      # no match
    '':   'No result',
}

def _smarty_action(status, dpv_code, smarty_addr):
    if status == 'API error':
        return 'API error - re-run verification; this address was not checked'
    if smarty_addr:
        return f'Partial USPS match - update address in Archtics to: {smarty_addr}'
    return 'No USPS match - contact patron to verify address, or remove from list'

def build_smarty_client(auth_id, auth_token):
    from smartystreets_python_sdk import StaticCredentials, ClientBuilder
    creds = StaticCredentials(auth_id, auth_token)
    return ClientBuilder(creds).build_us_street_api_client()

def verify_batch(client, rows, log_fn):
    """
    Call Smarty on up to 100 rows at a time.
    Returns list of dicts: {row, status, dpv_code, smarty_addr, smarty_zip4}
    """
    from smartystreets_python_sdk.us_street import Lookup
    from smartystreets_python_sdk import Batch

    results = []
    batch_size = 100

    for start in range(0, len(rows), batch_size):
        chunk = rows[start:start + batch_size]
        batch = Batch()

        for i, row in enumerate(chunk):
            lk = Lookup()
            lk.input_id    = str(i)
            lk.street      = row.get('street_addr_1', '')
            lk.street2     = row.get('street_addr_2', '')
            lk.city        = row.get('city', '')
            lk.state       = row.get('state', '')
            lk.zipcode     = normalize_zip(row.get('zip', ''))
            lk.candidates  = 1
            lk.match       = 'strict'
            batch.add(lk)

        try:
            client.send_batch(batch)
        except Exception as e:
            log_fn(f"  ⚠  Smarty API error on batch starting row {start}: {e}")
            # Mark all in this chunk as errors
            for row in chunk:
                results.append({
                    'row': row, 'status': 'API error',
                    'dpv_code': '', 'smarty_addr': '', 'smarty_zip4': '',
                })
            continue

        for i, row in enumerate(chunk):
            lk = batch[i]
            if lk.result:
                candidate = lk.result[0]
                dpv  = getattr(candidate.analysis, 'dpv_match_code', '') or ''
                addr = getattr(candidate, 'delivery_line_1', '')          or ''
                zip4 = ''
                if hasattr(candidate, 'components'):
                    zip4 = getattr(candidate.components, 'plus4_code', '') or ''
                results.append({
                    'row': row,
                    'status': DPV_LABELS.get(dpv, f'Code: {dpv}'),
                    'dpv_code': dpv,
                    'smarty_addr': addr,
                    'smarty_zip4': zip4,
                })
            else:
                results.append({
                    'row': row, 'status': 'Not confirmed',
                    'dpv_code': 'N', 'smarty_addr': '', 'smarty_zip4': '',
                })

        pct = min(start + batch_size, len(rows))
        log_fn(f"  Verified {pct:,} / {len(rows):,}…")

    return results


# ── Deduplication + optional verification engine ──────────────────────────────

def run_pipeline(input_path, output_path, flagged_path,
                 do_verify, auth_id, auth_token,
                 log_fn, done_fn):
    try:
        # ── Load ─────────────────────────────────────────────────────────────
        log_fn("Reading input file…")
        for _enc in ('utf-8-sig', 'cp1252', 'latin-1'):
            try:
                with open(input_path, newline='', encoding=_enc) as f:
                    reader     = csv.DictReader(f)
                    fieldnames = reader.fieldnames or []
                    rows       = list(reader)
                log_fn(f"  Encoding: {_enc}")
                break
            except UnicodeDecodeError:
                continue
        else:
            log_fn("ERROR: Could not read file — unknown encoding.")
            done_fn(None); return

        total_in = len(rows)
        log_fn(f"  Loaded {total_in:,} records.")
        if total_in == 0:
            log_fn("ERROR: File is empty or has no data rows.")
            done_fn(None); return

        # ── Dedup ─────────────────────────────────────────────────────────────
        log_fn("\nDeduplicating by address…")
        groups = {}
        no_addr = 0
        for row in rows:
            if not row.get('street_addr_1', '').strip():
                no_addr += 1
                continue
            key = make_dedup_key(row)
            groups.setdefault(key, []).append(row)

        if no_addr:
            log_fn(f"  ⚠  {no_addr} record(s) skipped — no street address.")

        dup_groups     = {k: v for k, v in groups.items() if len(v) > 1}
        records_removed = total_in - len(groups)
        log_fn(f"  {len(groups):,} unique addresses found.")
        log_fn(f"  {len(dup_groups):,} address groups had duplicates → {records_removed:,} records removed.")

        deduped = []
        dup_groups_list = []
        for group in groups.values():
            if len(group) == 1:
                deduped.append(group[0])
            else:
                sorted_group = sorted(group, key=_dedup_sort_key)
                deduped.append(sorted_group[0])
                dup_groups_list.append({'winner': sorted_group[0], 'removed': sorted_group[1:]})

        log_fn(f"  {len(deduped):,} records after dedup.")

        # ── Smarty verification (optional) ────────────────────────────────────
        confirmed   = deduped
        flagged     = []
        verify_stats = {}

        if do_verify:
            log_fn(f"\nConnecting to Smarty API…")
            try:
                client = build_smarty_client(auth_id.strip(), auth_token.strip())
                log_fn(f"  Verifying {len(deduped):,} addresses in batches of 100…")
                results = verify_batch(client, deduped, log_fn)
            except Exception as e:
                log_fn(f"\nERROR connecting to Smarty: {e}")
                log_fn("Skipping verification — dedup output will still be written.")
                results = []

            if results:
                confirmed, flagged = [], []
                cnt = {'Y': 0, 'S': 0, 'D': 0, 'N': 0, 'err': 0}
                for r in results:
                    dpv = r['dpv_code']
                    if dpv in ('Y', 'S', 'D'):
                        confirmed.append(r['row'])
                        cnt[dpv] += 1
                    else:
                        # Attach Smarty metadata to flagged rows for review
                        flagged_row = dict(r['row'])
                        flagged_row['smarty_status']   = r['status']
                        flagged_row['smarty_dpv_code'] = r['dpv_code']
                        flagged_row['smarty_addr']     = r['smarty_addr']
                        flagged_row['smarty_action']   = _smarty_action(
                            r['status'], r['dpv_code'], r['smarty_addr'])
                        flagged.append(flagged_row)
                        if r['status'] == 'API error':
                            cnt['err'] += 1
                        else:
                            cnt['N'] = cnt.get('N', 0) + 1

                verify_stats = {
                    'confirmed':    len(confirmed),
                    'flagged':      len(flagged),
                    'dpv_full':     cnt.get('Y', 0),
                    'dpv_partial':  cnt.get('S', 0) + cnt.get('D', 0),
                    'dpv_none':     cnt.get('N', 0),
                    'api_errors':   cnt.get('err', 0),
                }

                log_fn(f"\n  Smarty results:")
                log_fn(f"    Fully confirmed (Y):      {cnt.get('Y',0):>6,}")
                log_fn(f"    Confirmed / no unit (S/D):{cnt.get('S',0)+cnt.get('D',0):>6,}")
                log_fn(f"    Not confirmed (N):        {cnt.get('N',0):>6,}")
                if cnt.get('err'):
                    log_fn(f"    API errors:               {cnt['err']:>6,}")

        # ── Write confirmed output ────────────────────────────────────────────
        log_fn(f"\nWriting {len(confirmed):,} confirmed records…")
        with open(output_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(confirmed)

        # ── Write flagged output ──────────────────────────────────────────────
        if flagged:
            flagged_fields = list(fieldnames) + ['smarty_status', 'smarty_dpv_code', 'smarty_addr', 'smarty_action']
            log_fn(f"Writing {len(flagged):,} flagged records…")
            with open(flagged_path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=flagged_fields, extrasaction='ignore')
                writer.writeheader()
                writer.writerows(flagged)

        # ── Summary ───────────────────────────────────────────────────────────
        pct_removed = (records_removed / total_in * 100) if total_in else 0
        log_fn("\n" + "─" * 54)
        log_fn(f"  Input records:          {total_in:>8,}")
        if no_addr:
            log_fn(f"  Skipped (no address):   {no_addr:>8,}")
        log_fn(f"  Duplicate groups:       {len(dup_groups):>8,}")
        log_fn(f"  Removed (dedup):        {records_removed:>8,}  ({pct_removed:.1f}%)")
        if do_verify and verify_stats:
            pct_flag = (verify_stats['flagged'] / len(deduped) * 100) if deduped else 0
            log_fn(f"  Confirmed by Smarty:    {verify_stats['confirmed']:>8,}")
            log_fn(f"  Flagged (unverifiable): {verify_stats['flagged']:>8,}  ({pct_flag:.1f}%)")
        log_fn(f"  Final output records:   {len(confirmed):>8,}")
        log_fn("─" * 54)
        log_fn(f"✓  Saved: {output_path}")
        if flagged:
            log_fn(f"⚑  Flagged: {flagged_path}")

        stats = {
            'total_in':      total_in,
            'removed':       records_removed,
            'pct_removed':   pct_removed,
            'confirmed':     len(confirmed),
            'flagged':       len(flagged),
            'did_verify':    do_verify and bool(verify_stats),
            'dup_groups':    dup_groups_list,
            'fieldnames':    fieldnames,
        }
        done_fn(stats)

    except Exception as e:
        import traceback
        log_fn(f"\nERROR: {e}\n{traceback.format_exc()}")
        done_fn(None)


# ── UI ────────────────────────────────────────────────────────────────────────

_PRIMARY_BG = '#2d6a9f'
_PRIMARY_FG = 'white'

class DedupApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Mailing List Deduplication")
        self.geometry("700x660")
        self.resizable(True, True)
        self.minsize(580, 560)

        self.input_path   = tk.StringVar()
        self.output_path  = tk.StringVar()
        self.flagged_path = tk.StringVar()
        self.mode         = tk.StringVar(value="dedup")   # "dedup" | "verify"
        self.auth_id      = tk.StringVar()
        self.auth_token   = tk.StringVar()

        self._build_ui()

    # ── Layout ───────────────────────────────────────────────────────────────

    def _build_ui(self):
        PAD = dict(padx=20, pady=6)

        # Header bar
        hdr = tk.Frame(self, bg=_PRIMARY_BG)
        hdr.pack(fill=X)
        tk.Label(hdr, text="  Mailing List Deduplication",
                 font=("Segoe UI", 14, "bold"),
                 bg=_PRIMARY_BG, fg=_PRIMARY_FG, anchor=W
                 ).pack(side=LEFT, pady=12, padx=4)
        tk.Label(hdr, text="Fox Cities PAC – Ticket Services  ",
                 font=("Segoe UI", 9),
                 bg=_PRIMARY_BG, fg=_PRIMARY_FG, anchor=E
                 ).pack(side=RIGHT, pady=12)

        # ── Mode selector ─────────────────────────────────────────────────────
        mode_frame = ttk.LabelFrame(self, text="Mode", padding=10)
        mode_frame.pack(fill=X, **PAD)

        ttk.Radiobutton(
            mode_frame, text="Deduplicate only",
            variable=self.mode, value="dedup",
            command=self._on_mode_change
        ).pack(side=LEFT, padx=(0, 24))

        ttk.Radiobutton(
            mode_frame, text="Deduplicate + verify addresses (Smarty API)",
            variable=self.mode, value="verify",
            command=self._on_mode_change
        ).pack(side=LEFT)

        # ── Smarty credentials (shown only in verify mode) ────────────────────
        self.cred_frame = ttk.LabelFrame(self, text="Smarty API Credentials", padding=10)
        self.cred_frame.pack(fill=X, **PAD)

        ttk.Label(self.cred_frame, text="Auth ID:", width=12, anchor=E).grid(row=0, column=0, sticky=E, pady=3)
        ttk.Entry(self.cred_frame, textvariable=self.auth_id, width=42).grid(row=0, column=1, sticky=W, padx=8, pady=3)

        ttk.Label(self.cred_frame, text="Auth Token:", width=12, anchor=E).grid(row=1, column=0, sticky=E, pady=3)
        ttk.Entry(self.cred_frame, textvariable=self.auth_token, width=42, show="*").grid(row=1, column=1, sticky=W, padx=8, pady=3)

        tk.Label(self.cred_frame,
                 text="Keys available at smarty.com  —  free tier: 250 lookups/month",
                 font=("Segoe UI", 8), fg="#6c757d"
                 ).grid(row=2, column=0, columnspan=2, sticky=W, padx=8, pady=(2, 0))

        # ── File pickers ──────────────────────────────────────────────────────
        f_in = ttk.LabelFrame(self, text="Input File", padding=10)
        f_in.pack(fill=X, **PAD)
        ttk.Entry(f_in, textvariable=self.input_path, width=60).pack(side=LEFT, fill=X, expand=True, padx=(0, 8))
        ttk.Button(f_in, text="Browse…", command=self._browse_input).pack(side=LEFT)

        f_out = ttk.LabelFrame(self, text="Output — Deduplicated List", padding=10)
        self.f_out = f_out
        f_out.pack(fill=X, **PAD)
        ttk.Entry(f_out, textvariable=self.output_path, width=60).pack(side=LEFT, fill=X, expand=True, padx=(0, 8))
        ttk.Button(f_out, text="Browse…", command=self._browse_output).pack(side=LEFT)

        self.flag_frame = ttk.LabelFrame(self, text="Output — Flagged / Unverifiable Addresses", padding=10)
        self.flag_frame.pack(fill=X, **PAD)
        ttk.Entry(self.flag_frame, textvariable=self.flagged_path, width=60).pack(side=LEFT, fill=X, expand=True, padx=(0, 8))
        ttk.Button(self.flag_frame, text="Browse…", command=self._browse_flagged).pack(side=LEFT)

        # ── Run button + progress ─────────────────────────────────────────────
        btn_row = ttk.Frame(self)
        btn_row.pack(fill=X, padx=20, pady=4)
        self.run_btn = ttk.Button(btn_row, text="▶  Run", width=18, command=self._run)
        self.run_btn.pack(side=LEFT, padx=(0, 16))
        self.progress = ttk.Progressbar(btn_row, mode='indeterminate')
        self.progress.pack(side=LEFT, fill=X, expand=True)

        # ── Log / Duplicates notebook ─────────────────────────────────────────
        self.bottom_nb = ttk.Notebook(self)
        self.bottom_nb.pack(fill=BOTH, expand=True, padx=20, pady=(2, 4))

        log_tab = ttk.Frame(self.bottom_nb)
        self.bottom_nb.add(log_tab, text="Log")
        self.log_box = scrolledtext.ScrolledText(
            log_tab, font=("Consolas", 9), state='disabled',
            wrap='word', relief='flat', bg="#f8f9fa", fg="#212529")
        self.log_box.pack(fill=BOTH, expand=True)

        dup_tab = ttk.Frame(self.bottom_nb)
        self.bottom_nb.add(dup_tab, text="Removed Duplicates")
        self._build_dup_tab(dup_tab)

        # ── Status bar ────────────────────────────────────────────────────────
        self.status_var = tk.StringVar(value="Ready.")
        tk.Label(self, textvariable=self.status_var,
                 font=("Segoe UI", 8), fg="#6c757d", anchor=W
                 ).pack(fill=X, padx=20, pady=(0, 8))

        # Initial visibility
        self._on_mode_change()

    # ── Duplicates tab ────────────────────────────────────────────────────────

    _PREFERRED_COLS = ['acct_id', 'company_name', 'name', 'name_first',
                       'street_addr_1', 'city', 'state', 'zip', 'email_addr']

    def _build_dup_tab(self, parent):
        self.dup_tree = ttk.Treeview(parent, show='tree headings', selectmode='browse')
        vsb = ttk.Scrollbar(parent, orient=VERTICAL,   command=self.dup_tree.yview)
        hsb = ttk.Scrollbar(parent, orient=HORIZONTAL, command=self.dup_tree.xview)
        self.dup_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.dup_tree.grid(row=0, column=0, sticky='nsew')
        vsb.grid(row=0, column=1, sticky='ns')
        hsb.grid(row=1, column=0, sticky='ew')
        parent.grid_rowconfigure(0, weight=1)
        parent.grid_columnconfigure(0, weight=1)
        self.dup_tree.tag_configure('winner',  foreground='#1a5276')
        self.dup_tree.tag_configure('removed', foreground='#922b21')
        self.dup_tree.tag_configure('group',   font=('Segoe UI', 9, 'bold'))

    def _populate_duplicates(self, dup_groups, fieldnames):
        tree = self.dup_tree
        tree.delete(*tree.get_children())

        if not dup_groups:
            tree['columns'] = ('msg',)
            tree.column('#0',  width=0,   stretch=False)
            tree.column('msg', width=400, stretch=True)
            tree.heading('msg', text='')
            tree.insert('', END, values=('No duplicates were removed.',))
            return

        cols = [c for c in self._PREFERRED_COLS if c in fieldnames]
        if not cols:
            cols = list(fieldnames)[:8]

        tree['columns'] = ['status'] + cols
        tree.column('#0', width=220, stretch=False)
        tree.heading('#0', text='Group / Address')
        tree.heading('status', text='Status')
        tree.column('status', width=90, stretch=False, anchor=CENTER)
        for col in cols:
            tree.heading(col, text=col)
            tree.column(col, width=110, minwidth=60)

        def row_vals(r, label):
            return [label] + [r.get(c, '') for c in cols]

        for i, grp in enumerate(dup_groups, 1):
            w = grp['winner']
            addr_snip = (w.get('street_addr_1', '') + ', ' + w.get('city', '')).strip(', ')
            gid = tree.insert('', END, text=f"Group {i}  —  {addr_snip}",
                              open=True, tags=('group',))
            tree.insert(gid, END, values=row_vals(w, '✓ Kept'),    tags=('winner',))
            for rem in grp['removed']:
                tree.insert(gid, END, values=row_vals(rem, '✗ Removed'), tags=('removed',))

        self.bottom_nb.select(1)   # switch to duplicates tab

    # ── Mode change ───────────────────────────────────────────────────────────

    def _on_mode_change(self):
        show_verify = self.mode.get() == "verify"
        if show_verify:
            self.flag_frame.pack(fill=X, padx=20, pady=6, after=self.f_out)
        else:
            self.flag_frame.pack_forget()
        # Always keep cred_frame visible but disable fields in dedup mode
        for child in self.cred_frame.winfo_children():
            try:
                child.config(state='normal' if show_verify else 'disabled')
            except tk.TclError:
                pass

    # ── File pickers ─────────────────────────────────────────────────────────

    def _browse_input(self):
        path = filedialog.askopenfilename(
            title="Select mailing list CSV",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
        if path:
            self.input_path.set(path)
            p = Path(path)
            stamp = datetime.now().strftime("%Y%m%d")
            if not self.output_path.get():
                self.output_path.set(str(p.parent / f"{p.stem}_deduped_{stamp}.csv"))
            if not self.flagged_path.get():
                self.flagged_path.set(str(p.parent / f"{p.stem}_flagged_{stamp}.csv"))

    def _browse_output(self):
        path = filedialog.asksaveasfilename(
            title="Save deduplicated list as…",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv")])
        if path:
            self.output_path.set(path)

    def _browse_flagged(self):
        path = filedialog.asksaveasfilename(
            title="Save flagged addresses as…",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv")])
        if path:
            self.flagged_path.set(path)

    # ── Logging ──────────────────────────────────────────────────────────────

    def _log(self, msg):
        def _append():
            self.log_box.config(state='normal')
            self.log_box.insert('end', msg + '\n')
            self.log_box.see('end')
            self.log_box.config(state='disabled')
        self.after(0, _append)

    def _clear_log(self):
        self.log_box.config(state='normal')
        self.log_box.delete('1.0', 'end')
        self.log_box.config(state='disabled')

    # ── Run ──────────────────────────────────────────────────────────────────

    def _run(self):
        inp  = self.input_path.get().strip()
        out  = self.output_path.get().strip()
        flag = self.flagged_path.get().strip()
        mode = self.mode.get()
        do_verify = (mode == "verify")

        # Validation
        if not inp or not os.path.isfile(inp):
            self._log("⚠  Please select a valid input file."); return
        if not out:
            self._log("⚠  Please specify an output file path."); return
        if do_verify:
            if not self.auth_id.get().strip() or not self.auth_token.get().strip():
                self._log("⚠  Smarty Auth ID and Auth Token are required for verification."); return
            if not flag:
                self._log("⚠  Please specify a flagged-addresses output path."); return

        self._clear_log()
        self.run_btn.config(state='disabled')
        self.progress.start(10)

        stamp = datetime.now().strftime("%H:%M:%S")
        self._log(f"Started at {stamp}")
        self._log(f"Mode:   {'Deduplicate + Smarty verify' if do_verify else 'Deduplicate only'}")
        self._log(f"Input:  {inp}")
        self._log(f"Output: {out}")
        if do_verify:
            self._log(f"Flagged: {flag}")
        self._log("─" * 54)

        self.status_var.set("Running…")

        threading.Thread(
            target=run_pipeline,
            args=(inp, out, flag, do_verify,
                  self.auth_id.get(), self.auth_token.get(),
                  self._log, self._on_done),
            daemon=True
        ).start()

    def _on_done(self, stats):
        def _finish():
            self.progress.stop()
            self.run_btn.config(state='normal')
            if stats:
                parts = [f"{stats['confirmed']:,} records kept",
                         f"{stats['removed']:,} duplicates removed"]
                if stats.get('did_verify') and stats['flagged']:
                    parts.append(f"{stats['flagged']:,} flagged by Smarty")
                self.status_var.set("Done — " + ", ".join(parts) + ".")
                self._populate_duplicates(stats['dup_groups'], stats['fieldnames'])
            else:
                self.status_var.set("Finished with errors. See log above.")
        self.after(0, _finish)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    app = DedupApp()
    app.mainloop()
