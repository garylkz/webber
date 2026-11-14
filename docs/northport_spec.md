# Northport Enquiry Scraper: Specification

Container, bill of lading, vessel and booking enquiries against Northport's
e-NP2 CAS-N4 portal (Port Klang, Navis N4 data). This document describes what
each function does. Nothing here is specific to a language or runtime.

Available in [Python](../python/northport_scraper.py) and
[TypeScript](../typescript/northport_scraper.ts), with the same behaviour and
byte-identical output. Python needs `requests`; TypeScript has no
dependencies.

---

## Usage

The CLI is the same in both languages. Substitute the runner and the extension:

```bash
cd python        # or: cd typescript
python northport_scraper.py container ABCD1234567
python northport_scraper.py bl SOMEBL123
python northport_scraper.py vessel MV-EXAMPLE
python northport_scraper.py booking BOOKING123
```

Imported:

```python
import northport_scraper as northport

row = northport.get_by_container("ABCD1234567")
```

```ts
import { getByContainer } from './northport_scraper.ts';

const row = await getByContainer('ABCD1234567');
```

---

## Output

The portal has no schema. Each result is an object of the portal's own labels
mapped to their values, all strings, so which keys appear depends on the
enquiry. An unknown number gives `{}`. The first of a repeated label wins.

A booking adds `"Containers"`: a list of objects, each with `"Container
Number"` plus the rows under that container.

The CLI prints the object with indent 2.

---

## Functions

| Function | Returns | Description |
|---|---|---|
| `getByContainer(no)` | rows | One container. |
| `getByBl(no)` | rows | One bill of lading. |
| `getByVessel(scnOrName)` | rows | One vessel, by SCN or name. |
| `getByBooking(ref)` | rows + `Containers` | One booking and its container list. |
| `postForm(endpoint, field, value)` | `str` | The raw HTML fragment for one enquiry. |
| `parseRows(html)` | rows | Pure. A fragment's `Label : value` rows. |
| `parseBooking(html)` | rows + `Containers` | Pure. A booking fragment. |
| `toJson(result)` | `str` | Pure. The CLI's output. |

In Python the names are snake_case (`get_by_container`, `parse_rows`, ...).

---

## Endpoints

All are `POST` to `https://apps.northport.com.my/e-NP2/` with a form body of
one field, the value trimmed and upper-cased, and the header
`X-Requested-With: XMLHttpRequest`. No key, login or session.

| Enquiry | Page | Field |
|---|---|---|
| container | `query_contN4.php` | `cont_num_N4` |
| bl | `query_contblN4.php` | `cont_bl_N4` |
| vessel | `query_vesselN4.php` | `vsl_num_N4` |
| booking | `query_ebkref_N4.php` | `ebkref_num_N4` |

Each answers an HTML fragment of `<p>Label : <span>value</span></p>` rows. A
booking has header rows, then the text `Container List`, then one
`div_query_output_parent` (holding the container number) and
`div_query_output_child` (holding its rows) per container.
