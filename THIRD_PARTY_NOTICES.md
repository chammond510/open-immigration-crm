# Third-party software

Open Immigration CRM depends on unmodified open-source Python packages. The locked runtime set is listed in `requirements.lock`; development-only tools are listed in `requirements-dev.lock`.

Key runtime projects:

- Django — BSD-3-Clause
- dj-database-url — BSD-3-Clause
- Gunicorn — MIT
- psycopg — LGPL-3.0-only (library exception applies to linked applications; consult upstream terms)
- WhiteNoise — MIT

Container examples also use official Python and PostgreSQL images under their respective upstream terms. This notice is informational and does not replace the license files distributed by those projects.
