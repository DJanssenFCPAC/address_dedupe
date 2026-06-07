# Mailing List Deduplication Tool

**Owner:** Dallas Janssen, Ticket Services — Fox Cities PAC  
**Status:** Active  
**Last updated:** June 2026

---

## Purpose

Mailing lists exported from Archtics frequently contain multiple patron records at the same household address. This tool deduplicates those lists before physical mailings (season brochures, postcards, invitations) and optionally verifies deliverability against USPS data via the Smarty API.

---

## Files

| File | Description |
|---|---|
| `dedup.py` | Main application — run this |
| `PROJECT.md` | This file |

### Outputs (generated at runtime)

| File | Description |
|---|---|
| `<name>_deduped_YYYYMMDD.csv` | Clean list ready for mail house |
| `<name>_flagged_YYYYMMDD.csv` | Records Smarty could not verify — feed back into Archtics for cleanup |

---

## Requirements

```
pip install ttkbootstrap smartystreets-python-sdk
```

Python 3.10+ recommended. Tkinter must be available (`python3-tk` on Linux).

---

## Usage

```
python dedup.py
```

### Mode A — Deduplicate only

No API key needed. Normalizes addresses, groups records by household, keeps the most complete record per address group, and exports a single CSV.

### Mode B — Deduplicate + verify (Smarty API)

Runs dedup first, then passes the cleaned list to Smarty's US Street API in batches of 100. Records that Smarty cannot confirm as deliverable are written to a separate flagged CSV instead of the clean output.

Requires a Smarty account: [smarty.com](https://www.smarty.com/pricing)  
Free tier: 250 lookups/month. Pay-as-you-go ~$0.007/lookup beyond that (~$28 for a 4,000-row list).

---

## How it works

### Deduplication

1. **Address normalization** — uppercases, strips punctuation, standardizes abbreviations (Road → RD, Street → ST, County Road → CTY RD, directionals N/S/E/W, etc.) and normalizes ZIP to 5-digit base.
2. **Dedup key** — built from `street_addr_1 + city + zip`. PO Boxes include `street_addr_2` in the key so different box numbers at the same ZIP don't incorrectly merge.
3. **Winner selection** — when multiple records share an address, the keeper is chosen by:
   - **Completeness score** (most non-blank fields wins; bonus weight on `email_addr`, `phone_day`, `name_first`, `name`)
   - **Tiebreak:** lowest `acct_id` (longest patron relationship)

### Smarty verification

Each address in the deduped list is sent to Smarty's US Street API. The response includes a DPV (Delivery Point Validation) code:

| Code | Meaning | Disposition |
|---|---|---|
| `Y` | Fully confirmed | Clean output |
| `S` | Confirmed — secondary missing/invalid | Clean output |
| `D` | Confirmed — no secondary needed | Clean output |
| `N` | Not confirmed | Flagged output |
| _(no result)_ | No match found | Flagged output |

The flagged CSV includes three extra columns: `smarty_status`, `smarty_dpv_code`, and `smarty_addr` (Smarty's standardized version of the address) for Archtics cleanup reference.

---

## Edge cases handled

- **Apartment buildings** — unit numbers survive normalization; different units at the same street address are kept as separate records (correct for physical mail).
- **PO Boxes** — box number is included in the dedup key, so different organizations at the same ZIP with different box numbers are not merged.
- **Address formatting variants** — `"312 E Pershing St"` and `"312 E. Pershing St."` normalize to the same key and are correctly merged.
- **ZIP +4** — `54956-5006` and `54956` normalize to the same 5-digit base for matching.

---

## Known limitations

- Street type abbreviations that are fully spelled out vs. abbreviated (e.g., `"Road"` vs. `"Rd"`) are normalized, but uncommon variants may still slip through. Do a manual Find & Replace on `street_addr_1` for any patterns you notice recurring in your lists.
- Rural County Road formats (`County Road T`, `Cty Rd T`, `Co. Rd. T`) are normalized to `CTY RD T`, but local shorthand variants may need manual cleanup.
- Smarty validation is US addresses only.
- The tool expects the Fox Cities PAC Archtics column schema (`acct_id`, `street_addr_1`, `street_addr_2`, `city`, `state`, `zip`, etc.). Lists with different column names will dedup correctly but Smarty field mapping may need adjustment.

---

## Related procedure

A step-by-step **manual Excel procedure** covering the same deduplication logic (for cases where the Python tool isn't available) is documented in:

`MailingList_Dedup_Procedure.docx`

---

## Support / questions

Dallas Janssen — Ticket Services  
Fox Cities Performing Arts Center
