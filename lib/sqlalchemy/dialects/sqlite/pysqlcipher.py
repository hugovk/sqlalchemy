# sqlite/pysqlcipher.py
# Copyright (C) 2005-2021 the SQLAlchemy authors and contributors
# <see AUTHORS file>
#
# This module is part of SQLAlchemy and is released under
# the MIT License: https://www.opensource.org/licenses/mit-license.php

"""
.. dialect:: sqlite+pysqlcipher
    :name: pysqlcipher
    :dbapi: sqlcipher 3 or pysqlcipher
    :connectstring: sqlite+pysqlcipher://:passphrase@/file_path[?kdf_iter=<iter>]

    Dialect for support of DBAPIs that make use of the
    `SQLCipher <https://www.zetetic.net/sqlcipher>`_ backend.


Driver
------

Current dialect selection logic is:

* If the :paramref:`_sa.create_engine.module` parameter supplies a DBAPI module,
  that module is used.
* Otherwise for Python 3, choose https://pypi.org/project/sqlcipher3/
* If not available, fall back to https://pypi.org/project/pysqlcipher3/
* For Python 2, https://pypi.org/project/pysqlcipher/ is used.

.. warning:: The ``pysqlcipher3`` and ``pysqlcipher`` DBAPI drivers are no
   longer maintained; the ``sqlcipher3`` driver as of this writing appears
   to be current.  For future compatibility, any pysqlcipher-compatible DBAPI
   may be used as follows::

        import sqlcipher_compatible_driver

        from sqlalchemy import create_engine

        e = create_engine(
            "sqlite+pysqlcipher://:password@/dbname.db",
            module=sqlcipher_compatible_driver
        )

These drivers make use of the SQLCipher engine. This system essentially
introduces new PRAGMA commands to SQLite which allows the setting of a
passphrase and other encryption parameters, allowing the database file to be
encrypted.


Connect Strings
---------------

The format of the connect string is in every way the same as that
of the :mod:`~sqlalchemy.dialects.sqlite.pysqlite` driver, except that the
"password" field is now accepted, which should contain a passphrase::

    e = create_engine('sqlite+pysqlcipher://:testing@/foo.db')

For an absolute file path, two leading slashes should be used for the
database name::

    e = create_engine('sqlite+pysqlcipher://:testing@//path/to/foo.db')

A selection of additional encryption-related pragmas supported by SQLCipher
as documented at https://www.zetetic.net/sqlcipher/sqlcipher-api/ can be passed
in the query string, and will result in that PRAGMA being called for each
new connection.  Currently, ``cipher``, ``kdf_iter``
``cipher_page_size`` and ``cipher_use_hmac`` are supported::

    e = create_engine('sqlite+pysqlcipher://:testing@/foo.db?cipher=aes-256-cfb&kdf_iter=64000')

.. warning:: Previous versions of sqlalchemy did not take into consideration
   the encryption-related pragmas passed in the url string, that were silently
   ignored. This may cause errors when opening files saved by a
   previous sqlalchemy version if the encryption options do not match.


Pooling Behavior
----------------

The driver makes a change to the default pool behavior of pysqlite
as described in :ref:`pysqlite_threading_pooling`.   The pysqlcipher driver
has been observed to be significantly slower on connection than the
pysqlite driver, most likely due to the encryption overhead, so the
dialect here defaults to using the :class:`.SingletonThreadPool`
implementation,
instead of the :class:`.NullPool` pool used by pysqlite.  As always, the pool
implementation is entirely configurable using the
:paramref:`_sa.create_engine.poolclass` parameter; the :class:`.
StaticPool` may
be more feasible for single-threaded use, or :class:`.NullPool` may be used
to prevent unencrypted connections from being held open for long periods of
time, at the expense of slower startup time for new connections.


"""  # noqa

from .pysqlite import SQLiteDialect_pysqlite
from ... import pool


class SQLiteDialect_pysqlcipher(SQLiteDialect_pysqlite):
    driver = "pysqlcipher"
    supports_statement_cache = True

    pragmas = ("kdf_iter", "cipher", "cipher_page_size", "cipher_use_hmac")

    @classmethod
    def dbapi(cls):
        try:
            import sqlcipher3 as sqlcipher
        except ImportError:
            pass
        else:
            return sqlcipher

        from pysqlcipher3 import dbapi2 as sqlcipher

        return sqlcipher

    @classmethod
    def get_pool_class(cls, url):
        return pool.SingletonThreadPool

    def on_connect_url(self, url):
        super_on_connect = super().on_connect_url(url)

        # pull the info we need from the URL early.  Even though URL
        # is immutable, we don't want any in-place changes to the URL
        # to affect things
        passphrase = url.password or ""
        url_query = dict(url.query)

        def on_connect(conn):
            cursor = conn.cursor()
            cursor.execute('pragma key="%s"' % passphrase)
            for prag in self.pragmas:
                value = url_query.get(prag, None)
                if value is not None:
                    cursor.execute(f'pragma {prag}="{value}"')
            cursor.close()

            if super_on_connect:
                super_on_connect(conn)

        return on_connect

    def create_connect_args(self, url):
        plain_url = url._replace(password=None)
        plain_url = plain_url.difference_update_query(self.pragmas)
        return super().create_connect_args(plain_url)


dialect = SQLiteDialect_pysqlcipher
