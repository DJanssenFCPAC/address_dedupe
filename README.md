# Mailing List Deduplication
**Fox Cities PAC — Ticket Services**

---

## Purpose

Physical mail goes to a household or office, not an individual. When Archtics exports a patron list, the same address often appears under multiple account records — a patron who bought tickets individually, a joint spouse account, an old record with a different name spelling. Sending two or three pieces to the same address wastes postage and looks unprofessional.

This tool (and the manual Excel procedure below) reduces a raw Archtics export to one record per deliverable address before handing the list to the mail house.

---

## Deduplication philosophy

### The unit of mail is the address, not the person

Two accounts are considered the same household if they share the same normalized street address, city, and 5-digit ZIP. Name differences are ignored — the address is the only thing that matters for physical mail routing.

### Address normalization happens before comparison

Raw addresses from Archtics are inconsistent. `312 E. Pershing Street` and `312 E Pershing St` must compare equal or they produce two pieces of mail to the same door. Before any comparison the tool applies:

| Step | Example |
|---|---|
| Uppercase everything | `312 e pershing st` → `312 E PERSHING ST` |
| Strip periods | `E. Pershing St.` → `E Pershing St` |
| Collapse extra spaces | `312  E  Pershing` → `312 E Pershing` |
| Expand directionals | `North`, `South`, `East`, `West` → `N`, `S`, `E`, `W` |
| Expand street types | `Road`→`RD`, `Street`→`ST`, `Avenue`→`AVE`, `Drive`→`DR`, `Lane`→`LN`, `Court`→`CT`, `Boulevard`→`BLVD`, `Circle`→`CIR`, `Place`→`PL`, `Trail`→`TRL`, `Heights`→`HTS` |
| Normalize County Roads | `County Road`, `Cty Rd`, `Co Rd` → `CTY RD` |
| Normalize ZIP to 5 digits | `54956-5006` → `54956` |

PO Boxes are a special case: box number is included in the key, so `PO Box 100` and `PO Box 200` at the same ZIP are correctly kept as separate records.

### Records with no street address are dropped entirely

A record with a blank `street_addr_1` cannot receive physical mail and has no meaningful dedup key. These are removed before processing and reported in the log.

### When a group has duplicates, one winner is chosen

Priority order (highest to lowest):

1. **Company/organisation record first** — if one record in the group has a `company_name`, it wins over individual-name records. A company address typically represents a purchasing contact who should receive event marketing.
2. **Most complete record** — counted by number of non-blank fields, with extra weight on `company_name`, `email_addr`, `phone_day`, `name_first`, and `name`. A record with an email address is more valuable to retain than one without.
3. **Lowest `acct_id`** — tiebreaker. The lower the ID, the longer the patron relationship with the PAC.

---

## Column reference (Archtics export schema)

| Column | Used for |
|---|---|
| `acct_id` | Tiebreak (lower = older relationship) |
| `company_name` | Company priority; completeness bonus |
| `street_addr_1` | Primary dedup key component; must be non-blank |
| `street_addr_2` | Included in key for PO Boxes only |
| `city` | Dedup key component |
| `state` | Passed through; not used in key |
| `zip` | Dedup key component (5-digit base) |
| `name` | Completeness bonus |
| `name_first` | Completeness bonus |
| `phone_day` | Completeness bonus |
| `email_addr` | Completeness bonus |

---

## Replicating this manually in Excel

Use this procedure when the Python tool is not available. It follows the same logic.

### Setup

1. Open the Archtics CSV export in Excel.
2. **File → Save As** — save as `.xlsx` so formulas work.
3. Select row 1 → **View → Freeze Panes → Freeze Top Row**.
4. Note the column letters for your data. Based on the standard export order:

| Column | Letter |
|---|---|
| acct_id | A |
| company_name | B |
| street_addr_1 | C |
| street_addr_2 | D |
| phone_day | E |
| name | F |
| name_first | G |
| city | H |
| state | I |
| zip | J |
| email_addr | K |

---

### Step 1 — Remove no-address records

1. Click the `street_addr_1` column header (C) to select it.
2. **Data → Filter**.
3. Click the filter arrow → **Filter by condition → Is empty**.
4. Select all visible rows below the header → right-click → **Delete rows**.
5. Clear the filter (**Data → Filter** again to toggle off).

---

### Step 2 — Normalize street addresses

This brings `312 E. Pershing Street` and `312 E Pershing St` to the same value.

**A. Add a helper column** — insert a blank column after `street_addr_1` (right-click column D header → Insert). Label it `addr_norm` in row 1.

In D2 enter:
```
=TRIM(SUBSTITUTE(UPPER(C2),".",""))
```
Copy D2 down to all data rows.

**B. Apply abbreviation Find & Replace** — with column D selected, press **Ctrl+H**. Work through this list one row at a time:

| Find (use whole word — check "Match entire cell contents" OFF, "Match case" OFF) | Replace with |
|---|---|
| ` ROAD ` | ` RD ` |
| ` STREET ` | ` ST ` |
| ` AVENUE ` | ` AVE ` |
| ` DRIVE ` | ` DR ` |
| ` LANE ` | ` LN ` |
| ` COURT ` | ` CT ` |
| ` BOULEVARD ` | ` BLVD ` |
| ` CIRCLE ` | ` CIR ` |
| ` PLACE ` | ` PL ` |
| ` TRAIL ` | ` TRL ` |
| ` NORTH ` | ` N ` |
| ` SOUTH ` | ` S ` |
| ` EAST ` | ` E ` |
| ` WEST ` | ` W ` |
| `COUNTY ROAD` | `CTY RD` |

> **Tip:** include the surrounding spaces in the Find value to avoid replacing `NORTH` inside `NORTHFIELD`.

---

### Step 3 — Build the dedup key

Insert another blank column (label it `dedup_key`). Assuming `addr_norm` is now column D, `city` is I, and `zip` is K:

```
=D2&"|"&UPPER(TRIM(I2))&"|"&LEFT(TRIM(TEXT(K2,"00000")),5)
```

- `TEXT(K2,"00000")` preserves leading zeros on ZIP codes (e.g. `05401` not `5401`).
- Copy down to all rows.

---

### Step 4 — Build a completeness score

Insert a column labeled `score`. This approximates the Python tool's winner-selection logic:

```
=(LEN(TRIM(B2))>0)*3 + (LEN(TRIM(K2))>0)*2 + (LEN(TRIM(E2))>0)*2 + (LEN(TRIM(G2))>0)*2 + (LEN(TRIM(F2))>0)*2 + COUNTA(A2:K2)
```

Breakdown of bonuses:
- `company_name` non-blank → +3
- `email_addr` non-blank → +2
- `phone_day` non-blank → +2
- `name_first` non-blank → +2
- `name` non-blank → +2
- Base: count of all non-blank cells in the row

Copy down to all rows.

---

### Step 5 — Sort

**Data → Sort** — add levels in this exact order (Excel applies them top to bottom):

| Sort by | Order |
|---|---|
| `dedup_key` | A → Z |
| `score` | Largest to smallest |
| `acct_id` | Smallest to largest |

After sorting, all records at the same address are grouped together, with the best record at the top of each group.

---

### Step 6 — Mark duplicates

Insert a column labeled `keep`. In the first data row (row 2) enter:

```
=IF(ROW()=2,"KEEP",IF(E2=E1,"DUP","KEEP"))
```

Replace `E` with whichever column letter holds `dedup_key`. Copy down to all rows.

This marks the first record in each address group as `KEEP` and all subsequent ones as `DUP`.

---

### Step 7 — Delete duplicates

1. Filter the `keep` column to show only `DUP`.
2. Select all visible rows below the header.
3. Right-click → **Delete rows**.
4. Clear the filter.

---

### Step 8 — Clean up

Delete the helper columns (`addr_norm`, `dedup_key`, `score`, `keep`) before sending the file to the mail house. The column structure should match the original Archtics export.

---

### Step 9 — Spot check

Before delivering the file, scan for obvious issues:
- Any remaining blank `street_addr_1` cells? (Filter → Is empty)
- Any ZIP codes shorter than 5 digits? (Filter → Text length < 5)
- Do the record counts look right? Total rows should be noticeably fewer than the original.

---

## Outputs

| File | Description |
|---|---|
| `<name>_deduped_YYYYMMDD.csv` | Clean list, one record per address — send to mail house |
| `<name>_flagged_YYYYMMDD.csv` | Addresses Smarty could not confirm as deliverable (Mode B only) — feed back into Archtics for cleanup |

---

## Running the Python tool

```
python "C:\Tools\dedupe mailing\dedup.py"
```

**Requirements:** Python 3.10+, `pip install smartystreets-python-sdk`

**Mode A — Deduplicate only:** no API key needed.

**Mode B — Deduplicate + verify:** requires a free Smarty account (smarty.com). After deduplication, every address is checked against USPS data. Addresses Smarty cannot confirm as deliverable are written to the flagged file instead of the clean output. Free tier covers 250 lookups/month; pay-as-you-go is ~$0.007/lookup beyond that.

---

## Support

Dallas Janssen — Ticket Services, Fox Cities Performing Arts Center
