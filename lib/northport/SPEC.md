---
id: northport
type: module-design
status: draft
title: Northport Enquiry Scraper
parent: webber-architecture
---

# Northport Enquiry Scraper: Specification

Container, bill of lading, vessel and booking enquiries against Northport's
e-NP2 CAS-N4 portal (Port Klang, Navis N4 data). This document describes what
each function does.

Implemented in [northport_scraper.py](northport_scraper.py).

---

## Usage

The CLI:

```bash
cd lib/northport
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
| `get_by_container(container_no)` | rows | One container. |
| `get_by_bl(bl_no)` | rows | One bill of lading. |
| `get_by_vessel(scn_or_vessel_name)` | rows | One vessel, by SCN or name. |
| `get_by_booking(booking_ref)` | rows + `Containers` | One booking and its container list. |
| `post_form(endpoint, field, value)` | `str` | The raw HTML fragment for one enquiry. |
| `parse_rows(html)` | rows | Pure. A fragment's `Label : value` rows. |
| `parse_booking(html)` | rows + `Containers` | Pure. A booking fragment. |
| `to_json(result)` | `str` | Pure. The CLI's output. |

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
