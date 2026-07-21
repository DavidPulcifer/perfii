# Third-Party Notices

This file records the project's direct third-party dependencies as declared in
`current/requirements.txt`, `current/requirements-desktop.txt`, and the browser
assets loaded directly by the tracked templates. Versions and links were checked
on 2026-07-20.

This is a direct-dependency inventory, not a complete transitive, operating-system,
browser-runtime, or packaging inventory. Full terms remain in the linked upstream
license texts. Project licensing is set out in `LICENSE`; this inventory does not
replace upstream license terms and is not legal advice.

## Required web/runtime dependencies

These packages are pinned in `current/requirements.txt`.

| Component | Version | License | Upstream license and release metadata |
| --- | ---: | --- | --- |
| Flask | 3.1.3 | BSD-3-Clause | [License text](https://github.com/pallets/flask/blob/3.1.3/LICENSE.txt); [PyPI release](https://pypi.org/project/Flask/3.1.3/) |
| Gunicorn | 23.0.0 | MIT | [License text](https://github.com/benoitc/gunicorn/blob/23.0.0/LICENSE); [PyPI release](https://pypi.org/project/gunicorn/23.0.0/) |
| RapidFuzz | 3.14.5 | MIT | [License text](https://github.com/rapidfuzz/RapidFuzz/blob/v3.14.5/LICENSE); [PyPI release](https://pypi.org/project/RapidFuzz/3.14.5/) |

## Browser/CDN dependencies

These assets are loaded directly from jsDelivr by
`current/app/templates/layout.html` or `current/app/templates/invest.html`.

| Component | Selector in source | Resolved/recommended exact version | License | Upstream license and package metadata |
| --- | --- | ---: | --- | --- |
| Bootstrap | `bootstrap@5.3.3` | 5.3.3 | MIT | [License text](https://github.com/twbs/bootstrap/blob/v5.3.3/LICENSE); [npm package](https://www.npmjs.com/package/bootstrap/v/5.3.3) |
| Chart.js | `chart.js@4.4.1` | 4.4.1 | MIT | [License text](https://github.com/chartjs/Chart.js/blob/v4.4.1/LICENSE.md); [npm package](https://www.npmjs.com/package/chart.js/v/4.4.1) |
| chartjs-adapter-date-fns | `chartjs-adapter-date-fns@3.0.0` | 3.0.0 | MIT | [License text](https://github.com/chartjs/chartjs-adapter-date-fns/blob/v3.0.0/LICENSE.md); [npm package](https://www.npmjs.com/package/chartjs-adapter-date-fns/v/3.0.0) |
| Hammer.js | `hammerjs@2.0.8` | 2.0.8 | MIT | [License text](https://github.com/hammerjs/hammer.js/blob/master/LICENSE.md); [npm package](https://www.npmjs.com/package/hammerjs/v/2.0.8) |
| chartjs-plugin-zoom | `chartjs-plugin-zoom@2.2.0` | 2.2.0 | MIT | [License text](https://github.com/chartjs/chartjs-plugin-zoom/blob/v2.2.0/LICENSE.md); [npm package](https://www.npmjs.com/package/chartjs-plugin-zoom/v/2.2.0) |

### CDN reproducibility note

All current CDN dependencies use exact reviewed versions. The two URLs that
previously used major-only selectors are now pinned as:

- `https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js`
- `https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom@2.2.0/dist/chartjs-plugin-zoom.min.js`

The versions above were checked against official npm package metadata on the
inventory date. Keep these URLs exact when updating the corresponding templates.

## Optional desktop-conversion dependencies

These packages are explicitly pinned in `current/requirements-desktop.txt`. They
are not required for the shipped web application. A desktop conversion can also
introduce platform-specific runtimes and transitive packages that are outside the
scope of this direct-dependency list.

| Component | Version | License | Upstream license and release metadata |
| --- | ---: | --- | --- |
| Bottle | 0.13.4 | MIT | [License text](https://github.com/bottlepy/bottle/blob/0.13.4/LICENSE); [PyPI release](https://pypi.org/project/bottle/0.13.4/) |
| CFFI | 2.0.0 | MIT-0 wording (metadata declares MIT) | [Exact source license](https://github.com/python-cffi/cffi/blob/v2.0.0/LICENSE); [PyPI release](https://pypi.org/project/cffi/2.0.0/) |
| clr-loader | 0.2.7.post0 | MIT | [License text](https://github.com/pythonnet/clr-loader/blob/master/LICENSE); [PyPI release](https://pypi.org/project/clr-loader/0.2.7.post0/) |
| proxy_tools | 0.1.0 | BSD-2-Clause (source text; metadata declares MIT) | [License text](https://github.com/jtushman/proxy_tools/blob/master/LICENSE.txt); [PyPI release](https://pypi.org/project/proxy-tools/0.1.0/) |
| pycparser | 2.23 | BSD-3-Clause | [License text](https://github.com/eliben/pycparser/blob/release_v2.23/LICENSE); [PyPI release](https://pypi.org/project/pycparser/2.23/) |
| pythonnet | 3.0.5 | MIT | [License text](https://github.com/pythonnet/pythonnet/blob/v3.0.5/LICENSE); [PyPI release](https://pypi.org/project/pythonnet/3.0.5/) |
| pywebview | 6.0 | BSD-3-Clause | [License text](https://github.com/r0x0r/pywebview/blob/6.0/LICENSE); [PyPI release](https://pypi.org/project/pywebview/6.0/) |

### License metadata notes

CFFI 2.0.0 declares the SPDX expression `MIT` in its official PyPI metadata.
Its exact tagged `LICENSE` file is headed "MIT No Attribution" and omits the
standard MIT attribution condition; that wording corresponds to the commonly used
SPDX identifier `MIT-0`. The linked upstream license text records the actual terms.

`proxy_tools` 0.1.0 has a similar metadata mismatch in the other direction: PyPI
labels it MIT, while the upstream `LICENSE.txt` contains the two-clause BSD terms.
The table follows the upstream license text and records the metadata discrepancy.

## Scope maintenance

Update this inventory when a directly declared requirement or CDN import is added,
removed, or changed. A packaged desktop release needs a separate inventory of the
actual build output, including transitive Python packages, native runtimes, and any
vendored browser engine or installer components.
