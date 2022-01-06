# orm/mapper.py
# Copyright (C) 2005-2021 the SQLAlchemy authors and contributors
# <see AUTHORS file>
#
# This module is part of SQLAlchemy and is released under
# the MIT License: https://www.opensource.org/licenses/mit-license.php

"""Logic to map Python classes to and from selectables.

Defines the :class:`~sqlalchemy.orm.mapper.Mapper` class, the central
configurational unit which associates a class with a database table.

This is a semi-private module; the main configurational API of the ORM is
available in :class:`~sqlalchemy.orm.`.

"""

from collections import deque
from functools import reduce
from itertools import chain
import sys
import weakref

from . import attributes
from . import exc as orm_exc
from . import instrumentation
from . import loading
from . import properties
from . import util as orm_util
from .base import _class_to_mapper
from .base import _state_mapper
from .base import class_mapper
from .base import state_str
from .interfaces import _MappedAttribute
from .interfaces import EXT_SKIP
from .interfaces import InspectionAttr
from .interfaces import MapperProperty
from .interfaces import ORMEntityColumnsClauseRole
from .interfaces import ORMFromClauseRole
from .path_registry import PathRegistry
from .. import event
from .. import exc as sa_exc
from .. import inspection
from .. import log
from .. import schema
from .. import sql
from .. import util
from ..sql import base as sql_base
from ..sql import coercions
from ..sql import expression
from ..sql import operators
from ..sql import roles
from ..sql import util as sql_util
from ..sql import visitors
from ..sql.selectable import LABEL_STYLE_TABLENAME_PLUS_COL
from ..util import HasMemoized

_mapper_registries = weakref.WeakKeyDictionary()

_legacy_registry = None


def _all_registries():
    with _CONFIGURE_MUTEX:
        return set(_mapper_registries)


def _unconfigured_mappers():
    for reg in _all_registries():
        yield from reg._mappers_to_configure()


_already_compiling = False


# a constant returned by _get_attr_by_column to indicate
# this mapper is not handling an attribute for a particular
# column
NO_ATTRIBUTE = util.symbol("NO_ATTRIBUTE")

# lock used to synchronize the "mapper configure" step
_CONFIGURE_MUTEX = util.threading.RLock()


@inspection._self_inspects
@log.class_logger
class Mapper(
    ORMFromClauseRole,
    ORMEntityColumnsClauseRole,
    sql_base.MemoizedHasCacheKey,
    InspectionAttr,
):
    """Defines an association between a Python class and a database table or
    other relational structure, so that ORM operations against the class may
    proceed.

    The :class:`_orm.Mapper` object is instantiated using mapping methods
    present on the :class:`_orm.registry` object.  For information
    about instantiating new :class:`_orm.Mapper` objects, see
    :ref:`orm_mapping_classes_toplevel`.

    """

    _dispose_called = False
    _ready_for_configure = False

    @util.deprecated_params(
        non_primary=(
            "1.3",
            "The :paramref:`.mapper.non_primary` parameter is deprecated, "
            "and will be removed in a future release.  The functionality "
            "of non primary mappers is now better suited using the "
            ":class:`.AliasedClass` construct, which can also be used "
            "as the target of a :func:`_orm.relationship` in 1.3.",
        ),
    )
    def __init__(
        self,
        class_,
        local_table=None,
        properties=None,
        primary_key=None,
        non_primary=False,
        inherits=None,
        inherit_condition=None,
        inherit_foreign_keys=None,
        always_refresh=False,
        version_id_col=None,
        version_id_generator=None,
        polymorphic_on=None,
        _polymorphic_map=None,
        polymorphic_identity=None,
        concrete=False,
        with_polymorphic=None,
        polymorphic_load=None,
        allow_partial_pks=True,
        batch=True,
        column_prefix=None,
        include_properties=None,
        exclude_properties=None,
        passive_updates=True,
        passive_deletes=False,
        confirm_deleted_rows=True,
        eager_defaults=False,
        legacy_is_orphan=False,
        _compiled_cache_size=100,
    ):
        r"""Direct constructor for a new :class:`_orm.Mapper` object.

        The :func:`_orm.mapper` function is normally invoked through the
        use of the :class:`_orm.registry` object through either the
        :ref:`Declarative <orm_declarative_mapping>` or
        :ref:`Imperative <orm_imperative_mapping>` mapping styles.

        .. versionchanged:: 1.4 The :func:`_orm.mapper` function should not
           be called directly for classical mapping; for a classical mapping
           configuration, use the :meth:`_orm.registry.map_imperatively`
           method.   The :func:`_orm.mapper` function may become private in a
           future release.

        Parameters documented below may be passed to either the
        :meth:`_orm.registry.map_imperatively` method, or may be passed in the
        ``__mapper_args__`` declarative class attribute described at
        :ref:`orm_declarative_mapper_options`.

        :param class\_: The class to be mapped.  When using Declarative,
          this argument is automatically passed as the declared class
          itself.

        :param local_table: The :class:`_schema.Table` or other selectable
           to which the class is mapped.  May be ``None`` if
           this mapper inherits from another mapper using single-table
           inheritance.   When using Declarative, this argument is
           automatically passed by the extension, based on what
           is configured via the ``__table__`` argument or via the
           :class:`_schema.Table`
           produced as a result of the ``__tablename__``
           and :class:`_schema.Column` arguments present.

        :param always_refresh: If True, all query operations for this mapped
           class will overwrite all data within object instances that already
           exist within the session, erasing any in-memory changes with
           whatever information was loaded from the database. Usage of this
           flag is highly discouraged; as an alternative, see the method
           :meth:`_query.Query.populate_existing`.

        :param allow_partial_pks: Defaults to True.  Indicates that a
           composite primary key with some NULL values should be considered as
           possibly existing within the database. This affects whether a
           mapper will assign an incoming row to an existing identity, as well
           as if :meth:`.Session.merge` will check the database first for a
           particular primary key value. A "partial primary key" can occur if
           one has mapped to an OUTER JOIN, for example.

        :param batch: Defaults to ``True``, indicating that save operations
           of multiple entities can be batched together for efficiency.
           Setting to False indicates
           that an instance will be fully saved before saving the next
           instance.  This is used in the extremely rare case that a
           :class:`.MapperEvents` listener requires being called
           in between individual row persistence operations.

        :param column_prefix: A string which will be prepended
           to the mapped attribute name when :class:`_schema.Column`
           objects are automatically assigned as attributes to the
           mapped class.  Does not affect explicitly specified
           column-based properties.

           See the section :ref:`column_prefix` for an example.

        :param concrete: If True, indicates this mapper should use concrete
           table inheritance with its parent mapper.

           See the section :ref:`concrete_inheritance` for an example.

        :param confirm_deleted_rows: defaults to True; when a DELETE occurs
          of one more rows based on specific primary keys, a warning is
          emitted when the number of rows matched does not equal the number
          of rows expected.  This parameter may be set to False to handle the
          case where database ON DELETE CASCADE rules may be deleting some of
          those rows automatically.  The warning may be changed to an
          exception in a future release.

          .. versionadded:: 0.9.4 - added
             :paramref:`.mapper.confirm_deleted_rows` as well as conditional
             matched row checking on delete.

        :param eager_defaults: if True, the ORM will immediately fetch the
          value of server-generated default values after an INSERT or UPDATE,
          rather than leaving them as expired to be fetched on next access.
          This can be used for event schemes where the server-generated values
          are needed immediately before the flush completes.   By default,
          this scheme will emit an individual ``SELECT`` statement per row
          inserted or updated, which note can add significant performance
          overhead.  However, if the
          target database supports :term:`RETURNING`, the default values will
          be returned inline with the INSERT or UPDATE statement, which can
          greatly enhance performance for an application that needs frequent
          access to just-generated server defaults.

          .. seealso::

                :ref:`orm_server_defaults`

          .. versionchanged:: 0.9.0 The ``eager_defaults`` option can now
             make use of :term:`RETURNING` for backends which support it.

        :param exclude_properties: A list or set of string column names to
          be excluded from mapping.

          See :ref:`include_exclude_cols` for an example.

        :param include_properties: An inclusive list or set of string column
          names to map.

          See :ref:`include_exclude_cols` for an example.

        :param inherits: A mapped class or the corresponding
          :class:`_orm.Mapper`
          of one indicating a superclass to which this :class:`_orm.Mapper`
          should *inherit* from.   The mapped class here must be a subclass
          of the other mapper's class.   When using Declarative, this argument
          is passed automatically as a result of the natural class
          hierarchy of the declared classes.

          .. seealso::

            :ref:`inheritance_toplevel`

        :param inherit_condition: For joined table inheritance, a SQL
           expression which will
           define how the two tables are joined; defaults to a natural join
           between the two tables.

        :param inherit_foreign_keys: When ``inherit_condition`` is used and
           the columns present are missing a :class:`_schema.ForeignKey`
           configuration, this parameter can be used to specify which columns
           are "foreign".  In most cases can be left as ``None``.

        :param legacy_is_orphan: Boolean, defaults to ``False``.
          When ``True``, specifies that "legacy" orphan consideration
          is to be applied to objects mapped by this mapper, which means
          that a pending (that is, not persistent) object is auto-expunged
          from an owning :class:`.Session` only when it is de-associated
          from *all* parents that specify a ``delete-orphan`` cascade towards
          this mapper.  The new default behavior is that the object is
          auto-expunged when it is de-associated with *any* of its parents
          that specify ``delete-orphan`` cascade.  This behavior is more
          consistent with that of a persistent object, and allows behavior to
          be consistent in more scenarios independently of whether or not an
          orphan object has been flushed yet or not.

          See the change note and example at :ref:`legacy_is_orphan_addition`
          for more detail on this change.

        :param non_primary: Specify that this :class:`_orm.Mapper`
          is in addition
          to the "primary" mapper, that is, the one used for persistence.
          The :class:`_orm.Mapper` created here may be used for ad-hoc
          mapping of the class to an alternate selectable, for loading
          only.

         .. seealso::

            :ref:`relationship_aliased_class` - the new pattern that removes
            the need for the :paramref:`_orm.Mapper.non_primary` flag.

        :param passive_deletes: Indicates DELETE behavior of foreign key
           columns when a joined-table inheritance entity is being deleted.
           Defaults to ``False`` for a base mapper; for an inheriting mapper,
           defaults to ``False`` unless the value is set to ``True``
           on the superclass mapper.

           When ``True``, it is assumed that ON DELETE CASCADE is configured
           on the foreign key relationships that link this mapper's table
           to its superclass table, so that when the unit of work attempts
           to delete the entity, it need only emit a DELETE statement for the
           superclass table, and not this table.

           When ``False``, a DELETE statement is emitted for this mapper's
           table individually.  If the primary key attributes local to this
           table are unloaded, then a SELECT must be emitted in order to
           validate these attributes; note that the primary key columns
           of a joined-table subclass are not part of the "primary key" of
           the object as a whole.

           Note that a value of ``True`` is **always** forced onto the
           subclass mappers; that is, it's not possible for a superclass
           to specify passive_deletes without this taking effect for
           all subclass mappers.

           .. versionadded:: 1.1

           .. seealso::

               :ref:`passive_deletes` - description of similar feature as
               used with :func:`_orm.relationship`

               :paramref:`.mapper.passive_updates` - supporting ON UPDATE
               CASCADE for joined-table inheritance mappers

        :param passive_updates: Indicates UPDATE behavior of foreign key
           columns when a primary key column changes on a joined-table
           inheritance mapping.   Defaults to ``True``.

           When True, it is assumed that ON UPDATE CASCADE is configured on
           the foreign key in the database, and that the database will handle
           propagation of an UPDATE from a source column to dependent columns
           on joined-table rows.

           When False, it is assumed that the database does not enforce
           referential integrity and will not be issuing its own CASCADE
           operation for an update.  The unit of work process will
           emit an UPDATE statement for the dependent columns during a
           primary key change.

           .. seealso::

               :ref:`passive_updates` - description of a similar feature as
               used with :func:`_orm.relationship`

               :paramref:`.mapper.passive_deletes` - supporting ON DELETE
               CASCADE for joined-table inheritance mappers

        :param polymorphic_load: Specifies "polymorphic loading" behavior
          for a subclass in an inheritance hierarchy (joined and single
          table inheritance only).   Valid values are:

            * "'inline'" - specifies this class should be part of the
              "with_polymorphic" mappers, e.g. its columns will be included
              in a SELECT query against the base.

            * "'selectin'" - specifies that when instances of this class
              are loaded, an additional SELECT will be emitted to retrieve
              the columns specific to this subclass.  The SELECT uses
              IN to fetch multiple subclasses at once.

         .. versionadded:: 1.2

         .. seealso::

            :ref:`with_polymorphic_mapper_config`

            :ref:`polymorphic_selectin`

        :param polymorphic_on: Specifies the column, attribute, or
          SQL expression used to determine the target class for an
          incoming row, when inheriting classes are present.

          This value is commonly a :class:`_schema.Column` object that's
          present in the mapped :class:`_schema.Table`::

            class Employee(Base):
                __tablename__ = 'employee'

                id = Column(Integer, primary_key=True)
                discriminator = Column(String(50))

                __mapper_args__ = {
                    "polymorphic_on":discriminator,
                    "polymorphic_identity":"employee"
                }

          It may also be specified
          as a SQL expression, as in this example where we
          use the :func:`.case` construct to provide a conditional
          approach::

            class Employee(Base):
                __tablename__ = 'employee'

                id = Column(Integer, primary_key=True)
                discriminator = Column(String(50))

                __mapper_args__ = {
                    "polymorphic_on":case([
                        (discriminator == "EN", "engineer"),
                        (discriminator == "MA", "manager"),
                    ], else_="employee"),
                    "polymorphic_identity":"employee"
                }

          It may also refer to any attribute
          configured with :func:`.column_property`, or to the
          string name of one::

                class Employee(Base):
                    __tablename__ = 'employee'

                    id = Column(Integer, primary_key=True)
                    discriminator = Column(String(50))
                    employee_type = column_property(
                        case([
                            (discriminator == "EN", "engineer"),
                            (discriminator == "MA", "manager"),
                        ], else_="employee")
                    )

                    __mapper_args__ = {
                        "polymorphic_on":employee_type,
                        "polymorphic_identity":"employee"
                    }

          When setting ``polymorphic_on`` to reference an
          attribute or expression that's not present in the
          locally mapped :class:`_schema.Table`, yet the value
          of the discriminator should be persisted to the database,
          the value of the
          discriminator is not automatically set on new
          instances; this must be handled by the user,
          either through manual means or via event listeners.
          A typical approach to establishing such a listener
          looks like::

                from sqlalchemy import event
                from sqlalchemy.orm import object_mapper

                @event.listens_for(Employee, "init", propagate=True)
                def set_identity(instance, *arg, **kw):
                    mapper = object_mapper(instance)
                    instance.discriminator = mapper.polymorphic_identity

          Where above, we assign the value of ``polymorphic_identity``
          for the mapped class to the ``discriminator`` attribute,
          thus persisting the value to the ``discriminator`` column
          in the database.

          .. warning::

             Currently, **only one discriminator column may be set**, typically
             on the base-most class in the hierarchy. "Cascading" polymorphic
             columns are not yet supported.

          .. seealso::

            :ref:`inheritance_toplevel`

        :param polymorphic_identity: Specifies the value which
          identifies this particular class as returned by the
          column expression referred to by the ``polymorphic_on``
          setting.  As rows are received, the value corresponding
          to the ``polymorphic_on`` column expression is compared
          to this value, indicating which subclass should
          be used for the newly reconstructed object.

        :param properties: A dictionary mapping the string names of object
           attributes to :class:`.MapperProperty` instances, which define the
           persistence behavior of that attribute.  Note that
           :class:`_schema.Column`
           objects present in
           the mapped :class:`_schema.Table` are automatically placed into
           ``ColumnProperty`` instances upon mapping, unless overridden.
           When using Declarative, this argument is passed automatically,
           based on all those :class:`.MapperProperty` instances declared
           in the declared class body.

        :param primary_key: A list of :class:`_schema.Column`
           objects which define
           the primary key to be used against this mapper's selectable unit.
           This is normally simply the primary key of the ``local_table``, but
           can be overridden here.

        :param version_id_col: A :class:`_schema.Column`
           that will be used to keep a running version id of rows
           in the table.  This is used to detect concurrent updates or
           the presence of stale data in a flush.  The methodology is to
           detect if an UPDATE statement does not match the last known
           version id, a
           :class:`~sqlalchemy.orm.exc.StaleDataError` exception is
           thrown.
           By default, the column must be of :class:`.Integer` type,
           unless ``version_id_generator`` specifies an alternative version
           generator.

           .. seealso::

              :ref:`mapper_version_counter` - discussion of version counting
              and rationale.

        :param version_id_generator: Define how new version ids should
          be generated.  Defaults to ``None``, which indicates that
          a simple integer counting scheme be employed.  To provide a custom
          versioning scheme, provide a callable function of the form::

              def generate_version(version):
                  return next_version

          Alternatively, server-side versioning functions such as triggers,
          or programmatic versioning schemes outside of the version id
          generator may be used, by specifying the value ``False``.
          Please see :ref:`server_side_version_counter` for a discussion
          of important points when using this option.

          .. versionadded:: 0.9.0 ``version_id_generator`` supports
             server-side version number generation.

          .. seealso::

             :ref:`custom_version_counter`

             :ref:`server_side_version_counter`


        :param with_polymorphic: A tuple in the form ``(<classes>,
            <selectable>)`` indicating the default style of "polymorphic"
            loading, that is, which tables are queried at once. <classes> is
            any single or list of mappers and/or classes indicating the
            inherited classes that should be loaded at once. The special value
            ``'*'`` may be used to indicate all descending classes should be
            loaded immediately. The second tuple argument <selectable>
            indicates a selectable that will be used to query for multiple
            classes.

            .. seealso::

              :ref:`with_polymorphic` - discussion of polymorphic querying
              techniques.

        """
        self.class_ = util.assert_arg_type(class_, type, "class_")
        self._sort_key = "{}.{}".format(
            self.class_.__module__,
            self.class_.__name__,
        )

        self.class_manager = None

        self._primary_key_argument = util.to_list(primary_key)
        self.non_primary = non_primary

        self.always_refresh = always_refresh

        if isinstance(version_id_col, MapperProperty):
            self.version_id_prop = version_id_col
            self.version_id_col = None
        else:
            self.version_id_col = version_id_col
        if version_id_generator is False:
            self.version_id_generator = False
        elif version_id_generator is None:
            self.version_id_generator = lambda x: (x or 0) + 1
        else:
            self.version_id_generator = version_id_generator

        self.concrete = concrete
        self.single = False
        self.inherits = inherits
        if local_table is not None:
            self.local_table = coercions.expect(
                roles.StrictFromClauseRole, local_table
            )
        else:
            self.local_table = None

        self.inherit_condition = inherit_condition
        self.inherit_foreign_keys = inherit_foreign_keys
        self._init_properties = properties or {}
        self._delete_orphans = []
        self.batch = batch
        self.eager_defaults = eager_defaults
        self.column_prefix = column_prefix
        self.polymorphic_on = (
            coercions.expect(
                roles.ColumnArgumentOrKeyRole,
                polymorphic_on,
                argname="polymorphic_on",
            )
            if polymorphic_on is not None
            else None
        )
        self._dependency_processors = []
        self.validators = util.EMPTY_DICT
        self.passive_updates = passive_updates
        self.passive_deletes = passive_deletes
        self.legacy_is_orphan = legacy_is_orphan
        self._clause_adapter = None
        self._requires_row_aliasing = False
        self._inherits_equated_pairs = None
        self._memoized_values = {}
        self._compiled_cache_size = _compiled_cache_size
        self._reconstructor = None
        self.allow_partial_pks = allow_partial_pks

        if self.inherits and not self.concrete:
            self.confirm_deleted_rows = False
        else:
            self.confirm_deleted_rows = confirm_deleted_rows

        self._set_with_polymorphic(with_polymorphic)
        self.polymorphic_load = polymorphic_load

        # our 'polymorphic identity', a string name that when located in a
        #  result set row indicates this Mapper should be used to construct
        # the object instance for that row.
        self.polymorphic_identity = polymorphic_identity

        # a dictionary of 'polymorphic identity' names, associating those
        # names with Mappers that will be used to construct object instances
        # upon a select operation.
        if _polymorphic_map is None:
            self.polymorphic_map = {}
        else:
            self.polymorphic_map = _polymorphic_map

        if include_properties is not None:
            self.include_properties = util.to_set(include_properties)
        else:
            self.include_properties = None
        if exclude_properties:
            self.exclude_properties = util.to_set(exclude_properties)
        else:
            self.exclude_properties = None

        # prevent this mapper from being constructed
        # while a configure_mappers() is occurring (and defer a
        # configure_mappers() until construction succeeds)
        with _CONFIGURE_MUTEX:
            self.dispatch._events._new_mapper_instance(class_, self)
            self._configure_inheritance()
            self._configure_class_instrumentation()
            self._configure_properties()
            self._configure_polymorphic_setter()
            self._configure_pks()
            self.registry._flag_new_mapper(self)
            self._log("constructed")
            self._expire_memoizations()

    # major attributes initialized at the classlevel so that
    # they can be Sphinx-documented.

    is_mapper = True
    """Part of the inspection API."""

    represents_outer_join = False

    @property
    def mapper(self):
        """Part of the inspection API.

        Returns self.

        """
        return self

    def _gen_cache_key(self, anon_map, bindparams):
        return (self,)

    @property
    def entity(self):
        r"""Part of the inspection API.

        Returns self.class\_.

        """
        return self.class_

    local_table = None
    """The :class:`_expression.Selectable` which this :class:`_orm.Mapper`
    manages.

    Typically is an instance of :class:`_schema.Table` or
    :class:`_expression.Alias`.
    May also be ``None``.

    The "local" table is the
    selectable that the :class:`_orm.Mapper` is directly responsible for
    managing from an attribute access and flush perspective.   For
    non-inheriting mappers, the local table is the same as the
    "mapped" table.   For joined-table inheritance mappers, local_table
    will be the particular sub-table of the overall "join" which
    this :class:`_orm.Mapper` represents.  If this mapper is a
    single-table inheriting mapper, local_table will be ``None``.

    .. seealso::

        :attr:`_orm.Mapper.persist_selectable`.

    """

    persist_selectable = None
    """The :class:`_expression.Selectable` to which this :class:`_orm.Mapper`
    is mapped.

    Typically an instance of :class:`_schema.Table`,
    :class:`_expression.Join`, or :class:`_expression.Alias`.

    The :attr:`_orm.Mapper.persist_selectable` is separate from
    :attr:`_orm.Mapper.selectable` in that the former represents columns
    that are mapped on this class or its superclasses, whereas the
    latter may be a "polymorphic" selectable that contains additional columns
    which are in fact mapped on subclasses only.

    "persist selectable" is the "thing the mapper writes to" and
    "selectable" is the "thing the mapper selects from".

    :attr:`_orm.Mapper.persist_selectable` is also separate from
    :attr:`_orm.Mapper.local_table`, which represents the set of columns that
    are locally mapped on this class directly.


    .. seealso::

        :attr:`_orm.Mapper.selectable`.

        :attr:`_orm.Mapper.local_table`.

    """

    inherits = None
    """References the :class:`_orm.Mapper` which this :class:`_orm.Mapper`
    inherits from, if any.

    This is a *read only* attribute determined during mapper construction.
    Behavior is undefined if directly modified.

    """

    configured = False
    """Represent ``True`` if this :class:`_orm.Mapper` has been configured.

    This is a *read only* attribute determined during mapper construction.
    Behavior is undefined if directly modified.

    .. seealso::

        :func:`.configure_mappers`.

    """

    concrete = None
    """Represent ``True`` if this :class:`_orm.Mapper` is a concrete
    inheritance mapper.

    This is a *read only* attribute determined during mapper construction.
    Behavior is undefined if directly modified.

    """

    tables = None
    """An iterable containing the collection of :class:`_schema.Table` objects
    which this :class:`_orm.Mapper` is aware of.

    If the mapper is mapped to a :class:`_expression.Join`, or an
    :class:`_expression.Alias`
    representing a :class:`_expression.Select`, the individual
    :class:`_schema.Table`
    objects that comprise the full construct will be represented here.

    This is a *read only* attribute determined during mapper construction.
    Behavior is undefined if directly modified.

    """

    primary_key = None
    """An iterable containing the collection of :class:`_schema.Column`
    objects
    which comprise the 'primary key' of the mapped table, from the
    perspective of this :class:`_orm.Mapper`.

    This list is against the selectable in
    :attr:`_orm.Mapper.persist_selectable`.
    In the case of inheriting mappers, some columns may be managed by a
    superclass mapper.  For example, in the case of a
    :class:`_expression.Join`, the
    primary key is determined by all of the primary key columns across all
    tables referenced by the :class:`_expression.Join`.

    The list is also not necessarily the same as the primary key column
    collection associated with the underlying tables; the :class:`_orm.Mapper`
    features a ``primary_key`` argument that can override what the
    :class:`_orm.Mapper` considers as primary key columns.

    This is a *read only* attribute determined during mapper construction.
    Behavior is undefined if directly modified.

    """

    class_ = None
    """The Python class which this :class:`_orm.Mapper` maps.

    This is a *read only* attribute determined during mapper construction.
    Behavior is undefined if directly modified.

    """

    class_manager = None
    """The :class:`.ClassManager` which maintains event listeners
    and class-bound descriptors for this :class:`_orm.Mapper`.

    This is a *read only* attribute determined during mapper construction.
    Behavior is undefined if directly modified.

    """

    single = None
    """Represent ``True`` if this :class:`_orm.Mapper` is a single table
    inheritance mapper.

    :attr:`_orm.Mapper.local_table` will be ``None`` if this flag is set.

    This is a *read only* attribute determined during mapper construction.
    Behavior is undefined if directly modified.

    """

    non_primary = None
    """Represent ``True`` if this :class:`_orm.Mapper` is a "non-primary"
    mapper, e.g. a mapper that is used only to select rows but not for
    persistence management.

    This is a *read only* attribute determined during mapper construction.
    Behavior is undefined if directly modified.

    """

    polymorphic_on = None
    """The :class:`_schema.Column` or SQL expression specified as the
    ``polymorphic_on`` argument
    for this :class:`_orm.Mapper`, within an inheritance scenario.

    This attribute is normally a :class:`_schema.Column` instance but
    may also be an expression, such as one derived from
    :func:`.cast`.

    This is a *read only* attribute determined during mapper construction.
    Behavior is undefined if directly modified.

    """

    polymorphic_map = None
    """A mapping of "polymorphic identity" identifiers mapped to
    :class:`_orm.Mapper` instances, within an inheritance scenario.

    The identifiers can be of any type which is comparable to the
    type of column represented by :attr:`_orm.Mapper.polymorphic_on`.

    An inheritance chain of mappers will all reference the same
    polymorphic map object.  The object is used to correlate incoming
    result rows to target mappers.

    This is a *read only* attribute determined during mapper construction.
    Behavior is undefined if directly modified.

    """

    polymorphic_identity = None
    """Represent an identifier which is matched against the
    :attr:`_orm.Mapper.polymorphic_on` column during result row loading.

    Used only with inheritance, this object can be of any type which is
    comparable to the type of column represented by
    :attr:`_orm.Mapper.polymorphic_on`.

    This is a *read only* attribute determined during mapper construction.
    Behavior is undefined if directly modified.

    """

    base_mapper = None
    """The base-most :class:`_orm.Mapper` in an inheritance chain.

    In a non-inheriting scenario, this attribute will always be this
    :class:`_orm.Mapper`.   In an inheritance scenario, it references
    the :class:`_orm.Mapper` which is parent to all other :class:`_orm.Mapper`
    objects in the inheritance chain.

    This is a *read only* attribute determined during mapper construction.
    Behavior is undefined if directly modified.

    """

    columns = None
    """A collection of :class:`_schema.Column` or other scalar expression
    objects maintained by this :class:`_orm.Mapper`.

    The collection behaves the same as that of the ``c`` attribute on
    any :class:`_schema.Table` object,
    except that only those columns included in
    this mapping are present, and are keyed based on the attribute name
    defined in the mapping, not necessarily the ``key`` attribute of the
    :class:`_schema.Column` itself.   Additionally, scalar expressions mapped
    by :func:`.column_property` are also present here.

    This is a *read only* attribute determined during mapper construction.
    Behavior is undefined if directly modified.

    """

    validators = None
    """An immutable dictionary of attributes which have been decorated
    using the :func:`_orm.validates` decorator.

    The dictionary contains string attribute names as keys
    mapped to the actual validation method.

    """

    c = None
    """A synonym for :attr:`_orm.Mapper.columns`."""

    @property
    @util.deprecated("1.3", "Use .persist_selectable")
    def mapped_table(self):
        return self.persist_selectable

    @util.memoized_property
    def _path_registry(self) -> PathRegistry:
        return PathRegistry.per_mapper(self)

    def _configure_inheritance(self):
        """Configure settings related to inheriting and/or inherited mappers
        being present."""

        # a set of all mappers which inherit from this one.
        self._inheriting_mappers = util.WeakSequence()

        if self.inherits:
            if isinstance(self.inherits, type):
                self.inherits = class_mapper(self.inherits, configure=False)
            if not issubclass(self.class_, self.inherits.class_):
                raise sa_exc.ArgumentError(
                    "Class '%s' does not inherit from '%s'"
                    % (self.class_.__name__, self.inherits.class_.__name__)
                )

            self.dispatch._update(self.inherits.dispatch)

            if self.non_primary != self.inherits.non_primary:
                np = not self.non_primary and "primary" or "non-primary"
                raise sa_exc.ArgumentError(
                    "Inheritance of %s mapper for class '%s' is "
                    "only allowed from a %s mapper"
                    % (np, self.class_.__name__, np)
                )
            # inherit_condition is optional.
            if self.local_table is None:
                self.local_table = self.inherits.local_table
                self.persist_selectable = self.inherits.persist_selectable
                self.single = True
            elif self.local_table is not self.inherits.local_table:
                if self.concrete:
                    self.persist_selectable = self.local_table
                    for mapper in self.iterate_to_root():
                        if mapper.polymorphic_on is not None:
                            mapper._requires_row_aliasing = True
                else:
                    if self.inherit_condition is None:
                        # figure out inherit condition from our table to the
                        # immediate table of the inherited mapper, not its
                        # full table which could pull in other stuff we don't
                        # want (allows test/inheritance.InheritTest4 to pass)
                        try:
                            self.inherit_condition = sql_util.join_condition(
                                self.inherits.local_table, self.local_table
                            )
                        except sa_exc.NoForeignKeysError as nfe:
                            assert self.inherits.local_table is not None
                            assert self.local_table is not None
                            raise sa_exc.NoForeignKeysError(
                                "Can't determine the inherit condition "
                                "between inherited table '%s' and "
                                "inheriting "
                                "table '%s'; tables have no "
                                "foreign key relationships established.  "
                                "Please ensure the inheriting table has "
                                "a foreign key relationship to the "
                                "inherited "
                                "table, or provide an "
                                "'on clause' using "
                                "the 'inherit_condition' mapper argument."
                                % (
                                    self.inherits.local_table.description,
                                    self.local_table.description,
                                )
                            ) from nfe
                        except sa_exc.AmbiguousForeignKeysError as afe:
                            assert self.inherits.local_table is not None
                            assert self.local_table is not None
                            raise sa_exc.AmbiguousForeignKeysError(
                                "Can't determine the inherit condition "
                                "between inherited table '%s' and "
                                "inheriting "
                                "table '%s'; tables have more than one "
                                "foreign key relationship established.  "
                                "Please specify the 'on clause' using "
                                "the 'inherit_condition' mapper argument."
                                % (
                                    self.inherits.local_table.description,
                                    self.local_table.description,
                                )
                            ) from afe
                    self.persist_selectable = sql.join(
                        self.inherits.persist_selectable,
                        self.local_table,
                        self.inherit_condition,
                    )

                    fks = util.to_set(self.inherit_foreign_keys)
                    self._inherits_equated_pairs = sql_util.criterion_as_pairs(
                        self.persist_selectable.onclause,
                        consider_as_foreign_keys=fks,
                    )
            else:
                self.persist_selectable = self.local_table

            if self.polymorphic_identity is not None and not self.concrete:
                self._identity_class = self.inherits._identity_class
            else:
                self._identity_class = self.class_

            if self.version_id_col is None:
                self.version_id_col = self.inherits.version_id_col
                self.version_id_generator = self.inherits.version_id_generator
            elif (
                self.inherits.version_id_col is not None
                and self.version_id_col is not self.inherits.version_id_col
            ):
                util.warn(
                    "Inheriting version_id_col '%s' does not match inherited "
                    "version_id_col '%s' and will not automatically populate "
                    "the inherited versioning column. "
                    "version_id_col should only be specified on "
                    "the base-most mapper that includes versioning."
                    % (
                        self.version_id_col.description,
                        self.inherits.version_id_col.description,
                    )
                )

            self.polymorphic_map = self.inherits.polymorphic_map
            self.batch = self.inherits.batch
            self.inherits._inheriting_mappers.append(self)
            self.base_mapper = self.inherits.base_mapper
            self.passive_updates = self.inherits.passive_updates
            self.passive_deletes = (
                self.inherits.passive_deletes or self.passive_deletes
            )
            self._all_tables = self.inherits._all_tables

            if self.polymorphic_identity is not None:
                if self.polymorphic_identity in self.polymorphic_map:
                    util.warn(
                        "Reassigning polymorphic association for identity %r "
                        "from %r to %r: Check for duplicate use of %r as "
                        "value for polymorphic_identity."
                        % (
                            self.polymorphic_identity,
                            self.polymorphic_map[self.polymorphic_identity],
                            self,
                            self.polymorphic_identity,
                        )
                    )
                self.polymorphic_map[self.polymorphic_identity] = self

            if self.polymorphic_load and self.concrete:
                raise sa_exc.ArgumentError(
                    "polymorphic_load is not currently supported "
                    "with concrete table inheritance"
                )
            if self.polymorphic_load == "inline":
                self.inherits._add_with_polymorphic_subclass(self)
            elif self.polymorphic_load == "selectin":
                pass
            elif self.polymorphic_load is not None:
                raise sa_exc.ArgumentError(
                    "unknown argument for polymorphic_load: %r"
                    % self.polymorphic_load
                )

        else:
            self._all_tables = set()
            self.base_mapper = self
            self.persist_selectable = self.local_table
            if self.polymorphic_identity is not None:
                self.polymorphic_map[self.polymorphic_identity] = self
            self._identity_class = self.class_

        if self.persist_selectable is None:
            raise sa_exc.ArgumentError(
                "Mapper '%s' does not have a persist_selectable specified."
                % self
            )

    def _set_with_polymorphic(self, with_polymorphic):
        if with_polymorphic == "*":
            self.with_polymorphic = ("*", None)
        elif isinstance(with_polymorphic, (tuple, list)):
            if isinstance(with_polymorphic[0], (str, tuple, list)):
                self.with_polymorphic = with_polymorphic
            else:
                self.with_polymorphic = (with_polymorphic, None)
        elif with_polymorphic is not None:
            raise sa_exc.ArgumentError("Invalid setting for with_polymorphic")
        else:
            self.with_polymorphic = None

        if self.with_polymorphic and self.with_polymorphic[1] is not None:
            self.with_polymorphic = (
                self.with_polymorphic[0],
                coercions.expect(
                    roles.StrictFromClauseRole,
                    self.with_polymorphic[1],
                    allow_select=True,
                ),
            )

        if self.configured:
            self._expire_memoizations()

    def _add_with_polymorphic_subclass(self, mapper):
        subcl = mapper.class_
        if self.with_polymorphic is None:
            self._set_with_polymorphic((subcl,))
        elif self.with_polymorphic[0] != "*":
            self._set_with_polymorphic(
                (self.with_polymorphic[0] + (subcl,), self.with_polymorphic[1])
            )

    def _set_concrete_base(self, mapper):
        """Set the given :class:`_orm.Mapper` as the 'inherits' for this
        :class:`_orm.Mapper`, assuming this :class:`_orm.Mapper` is concrete
        and does not already have an inherits."""

        assert self.concrete
        assert not self.inherits
        assert isinstance(mapper, Mapper)
        self.inherits = mapper
        self.inherits.polymorphic_map.update(self.polymorphic_map)
        self.polymorphic_map = self.inherits.polymorphic_map
        for mapper in self.iterate_to_root():
            if mapper.polymorphic_on is not None:
                mapper._requires_row_aliasing = True
        self.batch = self.inherits.batch
        for mp in self.self_and_descendants:
            mp.base_mapper = self.inherits.base_mapper
        self.inherits._inheriting_mappers.append(self)
        self.passive_updates = self.inherits.passive_updates
        self._all_tables = self.inherits._all_tables

        for key, prop in mapper._props.items():
            if key not in self._props and not self._should_exclude(
                key, key, local=False, column=None
            ):
                self._adapt_inherited_property(key, prop, False)

    def _set_polymorphic_on(self, polymorphic_on):
        self.polymorphic_on = polymorphic_on
        self._configure_polymorphic_setter(True)

    def _configure_class_instrumentation(self):
        """If this mapper is to be a primary mapper (i.e. the
        non_primary flag is not set), associate this Mapper with the
        given class and entity name.

        Subsequent calls to ``class_mapper()`` for the ``class_`` / ``entity``
        name combination will return this mapper.  Also decorate the
        `__init__` method on the mapped class to include optional
        auto-session attachment logic.

        """

        # we expect that declarative has applied the class manager
        # already and set up a registry.  if this is None,
        # we will emit a deprecation warning below when we also see that
        # it has no registry.
        manager = attributes.manager_of_class(self.class_)

        if self.non_primary:
            if not manager or not manager.is_mapped:
                raise sa_exc.InvalidRequestError(
                    "Class %s has no primary mapper configured.  Configure "
                    "a primary mapper first before setting up a non primary "
                    "Mapper." % self.class_
                )
            self.class_manager = manager
            self.registry = manager.registry
            self._identity_class = manager.mapper._identity_class
            manager.registry._add_non_primary_mapper(self)
            return

        if manager is not None:
            assert manager.class_ is self.class_
            if manager.is_mapped:
                raise sa_exc.ArgumentError(
                    "Class '%s' already has a primary mapper defined. "
                    % self.class_
                )
            # else:
            # a ClassManager may already exist as
            # ClassManager.instrument_attribute() creates
            # new managers for each subclass if they don't yet exist.

        self.dispatch.instrument_class(self, self.class_)

        # this invokes the class_instrument event and sets up
        # the __init__ method.  documented behavior is that this must
        # occur after the instrument_class event above.
        # yes two events with the same two words reversed and different APIs.
        # :(

        manager = instrumentation.register_class(
            self.class_,
            mapper=self,
            expired_attribute_loader=util.partial(
                loading.load_scalar_attributes, self
            ),
            # finalize flag means instrument the __init__ method
            # and call the class_instrument event
            finalize=True,
        )

        if not manager.registry:
            util.warn_deprecated_20(
                "Calling the mapper() function directly outside of a "
                "declarative registry is deprecated."
                " Please use the sqlalchemy.orm.registry.map_imperatively() "
                "function for a classical mapping."
            )
            assert _legacy_registry is not None
            _legacy_registry._add_manager(manager)

        self.class_manager = manager
        self.registry = manager.registry

        # The remaining members can be added by any mapper,
        # e_name None or not.
        if manager.mapper is None:
            return

        event.listen(manager, "init", _event_on_init, raw=True)

        for key, method in util.iterate_attributes(self.class_):
            if key == "__init__" and hasattr(method, "_sa_original_init"):
                method = method._sa_original_init
                if hasattr(method, "__func__"):
                    method = method.__func__
            if callable(method):
                if hasattr(method, "__sa_reconstructor__"):
                    self._reconstructor = method
                    event.listen(manager, "load", _event_on_load, raw=True)
                elif hasattr(method, "__sa_validators__"):
                    validation_opts = method.__sa_validation_opts__
                    for name in method.__sa_validators__:
                        if name in self.validators:
                            raise sa_exc.InvalidRequestError(
                                "A validation function for mapped "
                                "attribute %r on mapper %s already exists."
                                % (name, self)
                            )
                        self.validators = self.validators.union(
                            {name: (method, validation_opts)}
                        )

    def _set_dispose_flags(self):
        self.configured = True
        self._ready_for_configure = True
        self._dispose_called = True

        self.__dict__.pop("_configure_failed", None)

    def _configure_pks(self):
        self.tables = sql_util.find_tables(self.persist_selectable)

        self._pks_by_table = {}
        self._cols_by_table = {}

        all_cols = util.column_set(
            chain(*[col.proxy_set for col in self._columntoproperty])
        )

        pk_cols = util.column_set(c for c in all_cols if c.primary_key)

        # identify primary key columns which are also mapped by this mapper.
        tables = set(self.tables + [self.persist_selectable])
        self._all_tables.update(tables)
        for t in tables:
            if t.primary_key and pk_cols.issuperset(t.primary_key):
                # ordering is important since it determines the ordering of
                # mapper.primary_key (and therefore query.get())
                self._pks_by_table[t] = util.ordered_column_set(
                    t.primary_key
                ).intersection(pk_cols)
            self._cols_by_table[t] = util.ordered_column_set(t.c).intersection(
                all_cols
            )

        # if explicit PK argument sent, add those columns to the
        # primary key mappings
        if self._primary_key_argument:
            for k in self._primary_key_argument:
                if k.table not in self._pks_by_table:
                    self._pks_by_table[k.table] = util.OrderedSet()
                self._pks_by_table[k.table].add(k)

        # otherwise, see that we got a full PK for the mapped table
        elif (
            self.persist_selectable not in self._pks_by_table
            or len(self._pks_by_table[self.persist_selectable]) == 0
        ):
            raise sa_exc.ArgumentError(
                "Mapper %s could not assemble any primary "
                "key columns for mapped table '%s'"
                % (self, self.persist_selectable.description)
            )
        elif self.local_table not in self._pks_by_table and isinstance(
            self.local_table, schema.Table
        ):
            util.warn(
                "Could not assemble any primary "
                "keys for locally mapped table '%s' - "
                "no rows will be persisted in this Table."
                % self.local_table.description
            )

        if (
            self.inherits
            and not self.concrete
            and not self._primary_key_argument
        ):
            # if inheriting, the "primary key" for this mapper is
            # that of the inheriting (unless concrete or explicit)
            self.primary_key = self.inherits.primary_key
        else:
            # determine primary key from argument or persist_selectable pks -
            # reduce to the minimal set of columns
            if self._primary_key_argument:
                primary_key = sql_util.reduce_columns(
                    [
                        self.persist_selectable.corresponding_column(c)
                        for c in self._primary_key_argument
                    ],
                    ignore_nonexistent_tables=True,
                )
            else:
                primary_key = sql_util.reduce_columns(
                    self._pks_by_table[self.persist_selectable],
                    ignore_nonexistent_tables=True,
                )

            if len(primary_key) == 0:
                raise sa_exc.ArgumentError(
                    "Mapper %s could not assemble any primary "
                    "key columns for mapped table '%s'"
                    % (self, self.persist_selectable.description)
                )

            self.primary_key = tuple(primary_key)
            self._log("Identified primary key columns: %s", primary_key)

        # determine cols that aren't expressed within our tables; mark these
        # as "read only" properties which are refreshed upon INSERT/UPDATE
        self._readonly_props = {
            self._columntoproperty[col]
            for col in self._columntoproperty
            if self._columntoproperty[col] not in self._identity_key_props
            and (
                not hasattr(col, "table")
                or col.table not in self._cols_by_table
            )
        }

    def _configure_properties(self):
        # Column and other ClauseElement objects which are mapped

        # TODO: technically this should be a DedupeColumnCollection
        # however DCC needs changes and more tests to fully cover
        # storing columns under a separate key name
        self.columns = self.c = sql_base.ColumnCollection()

        # object attribute names mapped to MapperProperty objects
        self._props = util.OrderedDict()

        # table columns mapped to lists of MapperProperty objects
        # using a list allows a single column to be defined as
        # populating multiple object attributes
        self._columntoproperty = _ColumnMapping(self)

        # load custom properties
        if self._init_properties:
            for key, prop in self._init_properties.items():
                self._configure_property(key, prop, False)

        # pull properties from the inherited mapper if any.
        if self.inherits:
            for key, prop in self.inherits._props.items():
                if key not in self._props and not self._should_exclude(
                    key, key, local=False, column=None
                ):
                    self._adapt_inherited_property(key, prop, False)

        # create properties for each column in the mapped table,
        # for those columns which don't already map to a property
        for column in self.persist_selectable.columns:
            if column in self._columntoproperty:
                continue

            column_key = (self.column_prefix or "") + column.key

            if self._should_exclude(
                column.key,
                column_key,
                local=self.local_table.c.contains_column(column),
                column=column,
            ):
                continue

            # adjust the "key" used for this column to that
            # of the inheriting mapper
            for mapper in self.iterate_to_root():
                if column in mapper._columntoproperty:
                    column_key = mapper._columntoproperty[column].key

            self._configure_property(
                column_key, column, init=False, setparent=True
            )

    def _configure_polymorphic_setter(self, init=False):
        """Configure an attribute on the mapper representing the
        'polymorphic_on' column, if applicable, and not
        already generated by _configure_properties (which is typical).

        Also create a setter function which will assign this
        attribute to the value of the 'polymorphic_identity'
        upon instance construction, also if applicable.  This
        routine will run when an instance is created.

        """
        setter = False

        if self.polymorphic_on is not None:
            setter = True

            if isinstance(self.polymorphic_on, str):
                # polymorphic_on specified as a string - link
                # it to mapped ColumnProperty
                try:
                    self.polymorphic_on = self._props[self.polymorphic_on]
                except KeyError as err:
                    raise sa_exc.ArgumentError(
                        "Can't determine polymorphic_on "
                        "value '%s' - no attribute is "
                        "mapped to this name." % self.polymorphic_on
                    ) from err

            if self.polymorphic_on in self._columntoproperty:
                # polymorphic_on is a column that is already mapped
                # to a ColumnProperty
                prop = self._columntoproperty[self.polymorphic_on]
            elif isinstance(self.polymorphic_on, MapperProperty):
                # polymorphic_on is directly a MapperProperty,
                # ensure it's a ColumnProperty
                if not isinstance(
                    self.polymorphic_on, properties.ColumnProperty
                ):
                    raise sa_exc.ArgumentError(
                        "Only direct column-mapped "
                        "property or SQL expression "
                        "can be passed for polymorphic_on"
                    )
                prop = self.polymorphic_on
            else:
                # polymorphic_on is a Column or SQL expression and
                # doesn't appear to be mapped. this means it can be 1.
                # only present in the with_polymorphic selectable or
                # 2. a totally standalone SQL expression which we'd
                # hope is compatible with this mapper's persist_selectable
                col = self.persist_selectable.corresponding_column(
                    self.polymorphic_on
                )
                if col is None:
                    # polymorphic_on doesn't derive from any
                    # column/expression isn't present in the mapped
                    # table. we will make a "hidden" ColumnProperty
                    # for it. Just check that if it's directly a
                    # schema.Column and we have with_polymorphic, it's
                    # likely a user error if the schema.Column isn't
                    # represented somehow in either persist_selectable or
                    # with_polymorphic.   Otherwise as of 0.7.4 we
                    # just go with it and assume the user wants it
                    # that way (i.e. a CASE statement)
                    setter = False
                    instrument = False
                    col = self.polymorphic_on
                    if isinstance(col, schema.Column) and (
                        self.with_polymorphic is None
                        or self.with_polymorphic[1].corresponding_column(col)
                        is None
                    ):
                        raise sa_exc.InvalidRequestError(
                            "Could not map polymorphic_on column "
                            "'%s' to the mapped table - polymorphic "
                            "loads will not function properly"
                            % col.description
                        )
                else:
                    # column/expression that polymorphic_on derives from
                    # is present in our mapped table
                    # and is probably mapped, but polymorphic_on itself
                    # is not.  This happens when
                    # the polymorphic_on is only directly present in the
                    # with_polymorphic selectable, as when use
                    # polymorphic_union.
                    # we'll make a separate ColumnProperty for it.
                    instrument = True
                key = getattr(col, "key", None)
                if key:
                    if self._should_exclude(col.key, col.key, False, col):
                        raise sa_exc.InvalidRequestError(
                            "Cannot exclude or override the "
                            "discriminator column %r" % col.key
                        )
                else:
                    self.polymorphic_on = col = col.label("_sa_polymorphic_on")
                    key = col.key

                prop = properties.ColumnProperty(col, _instrument=instrument)
                self._configure_property(key, prop, init=init, setparent=True)

            # the actual polymorphic_on should be the first public-facing
            # column in the property
            self.polymorphic_on = prop.columns[0]
            polymorphic_key = prop.key

        else:
            # no polymorphic_on was set.
            # check inheriting mappers for one.
            for mapper in self.iterate_to_root():
                # determine if polymorphic_on of the parent
                # should be propagated here.   If the col
                # is present in our mapped table, or if our mapped
                # table is the same as the parent (i.e. single table
                # inheritance), we can use it
                if mapper.polymorphic_on is not None:
                    if self.persist_selectable is mapper.persist_selectable:
                        self.polymorphic_on = mapper.polymorphic_on
                    else:
                        self.polymorphic_on = (
                            self.persist_selectable
                        ).corresponding_column(mapper.polymorphic_on)
                    # we can use the parent mapper's _set_polymorphic_identity
                    # directly; it ensures the polymorphic_identity of the
                    # instance's mapper is used so is portable to subclasses.
                    if self.polymorphic_on is not None:
                        self._set_polymorphic_identity = (
                            mapper._set_polymorphic_identity
                        )
                        self._validate_polymorphic_identity = (
                            mapper._validate_polymorphic_identity
                        )
                    else:
                        self._set_polymorphic_identity = None
                    return

        if setter:

            def _set_polymorphic_identity(state):
                dict_ = state.dict
                state.get_impl(polymorphic_key).set(
                    state,
                    dict_,
                    state.manager.mapper.polymorphic_identity,
                    None,
                )

            def _validate_polymorphic_identity(mapper, state, dict_):
                if (
                    polymorphic_key in dict_
                    and dict_[polymorphic_key]
                    not in mapper._acceptable_polymorphic_identities
                ):
                    util.warn_limited(
                        "Flushing object %s with "
                        "incompatible polymorphic identity %r; the "
                        "object may not refresh and/or load correctly",
                        (state_str(state), dict_[polymorphic_key]),
                    )

            self._set_polymorphic_identity = _set_polymorphic_identity
            self._validate_polymorphic_identity = (
                _validate_polymorphic_identity
            )
        else:
            self._set_polymorphic_identity = None

    _validate_polymorphic_identity = None

    @HasMemoized.memoized_attribute
    def _version_id_prop(self):
        if self.version_id_col is not None:
            return self._columntoproperty[self.version_id_col]
        else:
            return None

    @HasMemoized.memoized_attribute
    def _acceptable_polymorphic_identities(self):
        identities = set()

        stack = deque([self])
        while stack:
            item = stack.popleft()
            if item.persist_selectable is self.persist_selectable:
                identities.add(item.polymorphic_identity)
                stack.extend(item._inheriting_mappers)

        return identities

    @HasMemoized.memoized_attribute
    def _prop_set(self):
        return frozenset(self._props.values())

    @util.preload_module("sqlalchemy.orm.descriptor_props")
    def _adapt_inherited_property(self, key, prop, init):
        descriptor_props = util.preloaded.orm_descriptor_props

        if not self.concrete:
            self._configure_property(key, prop, init=False, setparent=False)
        elif key not in self._props:
            # determine if the class implements this attribute; if not,
            # or if it is implemented by the attribute that is handling the
            # given superclass-mapped property, then we need to report that we
            # can't use this at the instance level since we are a concrete
            # mapper and we don't map this.  don't trip user-defined
            # descriptors that might have side effects when invoked.
            implementing_attribute = self.class_manager._get_class_attr_mro(
                key, prop
            )
            if implementing_attribute is prop or (
                isinstance(
                    implementing_attribute, attributes.InstrumentedAttribute
                )
                and implementing_attribute._parententity is prop.parent
            ):
                self._configure_property(
                    key,
                    descriptor_props.ConcreteInheritedProperty(),
                    init=init,
                    setparent=True,
                )

    @util.preload_module("sqlalchemy.orm.descriptor_props")
    def _configure_property(self, key, prop, init=True, setparent=True):
        descriptor_props = util.preloaded.orm_descriptor_props
        self._log("_configure_property(%s, %s)", key, prop.__class__.__name__)

        if not isinstance(prop, MapperProperty):
            prop = self._property_from_column(key, prop)

        if isinstance(prop, properties.ColumnProperty):
            col = self.persist_selectable.corresponding_column(prop.columns[0])

            # if the column is not present in the mapped table,
            # test if a column has been added after the fact to the
            # parent table (or their parent, etc.) [ticket:1570]
            if col is None and self.inherits:
                path = [self]
                for m in self.inherits.iterate_to_root():
                    col = m.local_table.corresponding_column(prop.columns[0])
                    if col is not None:
                        for m2 in path:
                            m2.persist_selectable._refresh_for_new_column(col)
                        col = self.persist_selectable.corresponding_column(
                            prop.columns[0]
                        )
                        break
                    path.append(m)

            # subquery expression, column not present in the mapped
            # selectable.
            if col is None:
                col = prop.columns[0]

                # column is coming in after _readonly_props was
                # initialized; check for 'readonly'
                if hasattr(self, "_readonly_props") and (
                    not hasattr(col, "table")
                    or col.table not in self._cols_by_table
                ):
                    self._readonly_props.add(prop)

            else:
                # if column is coming in after _cols_by_table was
                # initialized, ensure the col is in the right set
                if (
                    hasattr(self, "_cols_by_table")
                    and col.table in self._cols_by_table
                    and col not in self._cols_by_table[col.table]
                ):
                    self._cols_by_table[col.table].add(col)

            # if this properties.ColumnProperty represents the "polymorphic
            # discriminator" column, mark it.  We'll need this when rendering
            # columns in SELECT statements.
            if not hasattr(prop, "_is_polymorphic_discriminator"):
                prop._is_polymorphic_discriminator = (
                    col is self.polymorphic_on
                    or prop.columns[0] is self.polymorphic_on
                )

            if isinstance(col, expression.Label):
                # new in 1.4, get column property against expressions
                # to be addressable in subqueries
                col.key = col._tq_key_label = key

            self.columns.add(col, key)
            for col in prop.columns + prop._orig_columns:
                for col in col.proxy_set:
                    self._columntoproperty[col] = prop

        prop.key = key

        if setparent:
            prop.set_parent(self, init)

        if key in self._props and getattr(
            self._props[key], "_mapped_by_synonym", False
        ):
            syn = self._props[key]._mapped_by_synonym
            raise sa_exc.ArgumentError(
                "Can't call map_column=True for synonym %r=%r, "
                "a ColumnProperty already exists keyed to the name "
                "%r for column %r" % (syn, key, key, syn)
            )

        if (
            key in self._props
            and not isinstance(prop, properties.ColumnProperty)
            and not isinstance(
                self._props[key],
                (
                    properties.ColumnProperty,
                    descriptor_props.ConcreteInheritedProperty,
                ),
            )
        ):
            util.warn(
                "Property %s on %s being replaced with new "
                "property %s; the old property will be discarded"
                % (self._props[key], self, prop)
            )
            oldprop = self._props[key]
            self._path_registry.pop(oldprop, None)

        self._props[key] = prop

        if not self.non_primary:
            prop.instrument_class(self)

        for mapper in self._inheriting_mappers:
            mapper._adapt_inherited_property(key, prop, init)

        if init:
            prop.init()
            prop.post_instrument_class(self)

        if self.configured:
            self._expire_memoizations()

    @util.preload_module("sqlalchemy.orm.descriptor_props")
    def _property_from_column(self, key, prop):
        """generate/update a :class:`.ColumnProperty` given a
        :class:`_schema.Column` object."""
        descriptor_props = util.preloaded.orm_descriptor_props
        # we were passed a Column or a list of Columns;
        # generate a properties.ColumnProperty
        columns = util.to_list(prop)
        column = columns[0]
        assert isinstance(column, expression.ColumnElement)

        prop = self._props.get(key, None)

        if isinstance(prop, properties.ColumnProperty):
            if (
                (
                    not self._inherits_equated_pairs
                    or (prop.columns[0], column)
                    not in self._inherits_equated_pairs
                )
                and not prop.columns[0].shares_lineage(column)
                and prop.columns[0] is not self.version_id_col
                and column is not self.version_id_col
            ):
                warn_only = prop.parent is not self
                msg = (
                    "Implicitly combining column %s with column "
                    "%s under attribute '%s'.  Please configure one "
                    "or more attributes for these same-named columns "
                    "explicitly." % (prop.columns[-1], column, key)
                )
                if warn_only:
                    util.warn(msg)
                else:
                    raise sa_exc.InvalidRequestError(msg)

            # existing properties.ColumnProperty from an inheriting
            # mapper. make a copy and append our column to it
            prop = prop.copy()
            prop.columns.insert(0, column)
            self._log(
                "inserting column to existing list "
                "in properties.ColumnProperty %s" % (key)
            )
            return prop
        elif prop is None or isinstance(
            prop, descriptor_props.ConcreteInheritedProperty
        ):
            mapped_column = []
            for c in columns:
                mc = self.persist_selectable.corresponding_column(c)
                if mc is None:
                    mc = self.local_table.corresponding_column(c)
                    if mc is not None:
                        # if the column is in the local table but not the
                        # mapped table, this corresponds to adding a
                        # column after the fact to the local table.
                        # [ticket:1523]
                        self.persist_selectable._refresh_for_new_column(mc)
                    mc = self.persist_selectable.corresponding_column(c)
                    if mc is None:
                        raise sa_exc.ArgumentError(
                            "When configuring property '%s' on %s, "
                            "column '%s' is not represented in the mapper's "
                            "table. Use the `column_property()` function to "
                            "force this column to be mapped as a read-only "
                            "attribute." % (key, self, c)
                        )
                mapped_column.append(mc)
            return properties.ColumnProperty(*mapped_column)
        else:
            raise sa_exc.ArgumentError(
                "WARNING: when configuring property '%s' on %s, "
                "column '%s' conflicts with property '%r'. "
                "To resolve this, map the column to the class under a "
                "different name in the 'properties' dictionary.  Or, "
                "to remove all awareness of the column entirely "
                "(including its availability as a foreign key), "
                "use the 'include_properties' or 'exclude_properties' "
                "mapper arguments to control specifically which table "
                "columns get mapped." % (key, self, column.key, prop)
            )

    def _check_configure(self):
        if self.registry._new_mappers:
            _configure_registries({self.registry}, cascade=True)

    def _post_configure_properties(self):
        """Call the ``init()`` method on all ``MapperProperties``
        attached to this mapper.

        This is a deferred configuration step which is intended
        to execute once all mappers have been constructed.

        """

        self._log("_post_configure_properties() started")
        l = [(key, prop) for key, prop in self._props.items()]
        for key, prop in l:
            self._log("initialize prop %s", key)

            if prop.parent is self and not prop._configure_started:
                prop.init()

            if prop._configure_finished:
                prop.post_instrument_class(self)

        self._log("_post_configure_properties() complete")
        self.configured = True

    def add_properties(self, dict_of_properties):
        """Add the given dictionary of properties to this mapper,
        using `add_property`.

        """
        for key, value in dict_of_properties.items():
            self.add_property(key, value)

    def add_property(self, key, prop):
        """Add an individual MapperProperty to this mapper.

        If the mapper has not been configured yet, just adds the
        property to the initial properties dictionary sent to the
        constructor.  If this Mapper has already been configured, then
        the given MapperProperty is configured immediately.

        """
        self._init_properties[key] = prop
        self._configure_property(key, prop, init=self.configured)

    def _expire_memoizations(self):
        for mapper in self.iterate_to_root():
            mapper._reset_memoizations()

    @property
    def _log_desc(self):
        return (
            "("
            + self.class_.__name__
            + "|"
            + (
                self.local_table is not None
                and self.local_table.description
                or str(self.local_table)
            )
            + (self.non_primary and "|non-primary" or "")
            + ")"
        )

    def _log(self, msg, *args):
        self.logger.info("%s " + msg, *((self._log_desc,) + args))

    def _log_debug(self, msg, *args):
        self.logger.debug("%s " + msg, *((self._log_desc,) + args))

    def __repr__(self):
        return f"<Mapper at 0x{id(self):x}; {self.class_.__name__}>"

    def __str__(self):
        return "Mapper[{}{}({})]".format(
            self.class_.__name__,
            self.non_primary and " (non-primary)" or "",
            self.local_table.description
            if self.local_table is not None
            else self.persist_selectable.description,
        )

    def _is_orphan(self, state):
        orphan_possible = False
        for mapper in self.iterate_to_root():
            for (key, cls) in mapper._delete_orphans:
                orphan_possible = True

                has_parent = attributes.manager_of_class(cls).has_parent(
                    state, key, optimistic=state.has_identity
                )

                if self.legacy_is_orphan and has_parent:
                    return False
                elif not self.legacy_is_orphan and not has_parent:
                    return True

        if self.legacy_is_orphan:
            return orphan_possible
        else:
            return False

    def has_property(self, key):
        return key in self._props

    def get_property(self, key, _configure_mappers=True):
        """return a MapperProperty associated with the given key."""

        if _configure_mappers:
            self._check_configure()

        try:
            return self._props[key]
        except KeyError as err:
            raise sa_exc.InvalidRequestError(
                f"Mapper '{self}' has no property '{key}'"
            ) from err

    def get_property_by_column(self, column):
        """Given a :class:`_schema.Column` object, return the
        :class:`.MapperProperty` which maps this column."""

        return self._columntoproperty[column]

    @property
    def iterate_properties(self):
        """return an iterator of all MapperProperty objects."""

        self._check_configure()
        return iter(self._props.values())

    def _mappers_from_spec(self, spec, selectable):
        """given a with_polymorphic() argument, return the set of mappers it
        represents.

        Trims the list of mappers to just those represented within the given
        selectable, if present. This helps some more legacy-ish mappings.

        """
        if spec == "*":
            mappers = list(self.self_and_descendants)
        elif spec:
            mappers = set()
            for m in util.to_list(spec):
                m = _class_to_mapper(m)
                if not m.isa(self):
                    raise sa_exc.InvalidRequestError(
                        f"{m!r} does not inherit from {self!r}"
                    )

                if selectable is None:
                    mappers.update(m.iterate_to_root())
                else:
                    mappers.add(m)
            mappers = [m for m in self.self_and_descendants if m in mappers]
        else:
            mappers = []

        if selectable is not None:
            tables = set(
                sql_util.find_tables(selectable, include_aliases=True)
            )
            mappers = [m for m in mappers if m.local_table in tables]
        return mappers

    def _selectable_from_mappers(self, mappers, innerjoin):
        """given a list of mappers (assumed to be within this mapper's
        inheritance hierarchy), construct an outerjoin amongst those mapper's
        mapped tables.

        """
        from_obj = self.persist_selectable
        for m in mappers:
            if m is self:
                continue
            if m.concrete:
                raise sa_exc.InvalidRequestError(
                    "'with_polymorphic()' requires 'selectable' argument "
                    "when concrete-inheriting mappers are used."
                )
            elif not m.single:
                if innerjoin:
                    from_obj = from_obj.join(
                        m.local_table, m.inherit_condition
                    )
                else:
                    from_obj = from_obj.outerjoin(
                        m.local_table, m.inherit_condition
                    )

        return from_obj

    @HasMemoized.memoized_attribute
    def _single_table_criterion(self):
        if self.single and self.inherits and self.polymorphic_on is not None:
            return self.polymorphic_on._annotate({"parentmapper": self}).in_(
                m.polymorphic_identity for m in self.self_and_descendants
            )
        else:
            return None

    @HasMemoized.memoized_attribute
    def _with_polymorphic_mappers(self):
        self._check_configure()

        if not self.with_polymorphic:
            return []
        return self._mappers_from_spec(*self.with_polymorphic)

    @HasMemoized.memoized_attribute
    def _post_inspect(self):
        """This hook is invoked by attribute inspection.

        E.g. when Query calls:

            coercions.expect(roles.ColumnsClauseRole, ent, keep_inspect=True)

        This allows the inspection process run a configure mappers hook.

        """
        self._check_configure()

    @HasMemoized.memoized_attribute
    def _with_polymorphic_selectable(self):
        if not self.with_polymorphic:
            return self.persist_selectable

        spec, selectable = self.with_polymorphic
        if selectable is not None:
            return selectable
        else:
            return self._selectable_from_mappers(
                self._mappers_from_spec(spec, selectable), False
            )

    with_polymorphic_mappers = _with_polymorphic_mappers
    """The list of :class:`_orm.Mapper` objects included in the
    default "polymorphic" query.

    """

    @HasMemoized.memoized_attribute
    def _insert_cols_evaluating_none(self):
        return {
            table: frozenset(
                col for col in columns if col.type.should_evaluate_none
            )
            for table, columns in self._cols_by_table.items()
        }

    @HasMemoized.memoized_attribute
    def _insert_cols_as_none(self):
        return {
            table: frozenset(
                col.key
                for col in columns
                if not col.primary_key
                and not col.server_default
                and not col.default
                and not col.type.should_evaluate_none
            )
            for table, columns in self._cols_by_table.items()
        }

    @HasMemoized.memoized_attribute
    def _propkey_to_col(self):
        return {
            table: {self._columntoproperty[col].key: col for col in columns}
            for table, columns in self._cols_by_table.items()
        }

    @HasMemoized.memoized_attribute
    def _pk_keys_by_table(self):
        return {
            table: frozenset(col.key for col in pks)
            for table, pks in self._pks_by_table.items()
        }

    @HasMemoized.memoized_attribute
    def _pk_attr_keys_by_table(self):
        return {
            table: frozenset(self._columntoproperty[col].key for col in pks)
            for table, pks in self._pks_by_table.items()
        }

    @HasMemoized.memoized_attribute
    def _server_default_cols(self):
        return {
            table: frozenset(
                col.key for col in columns if col.server_default is not None
            )
            for table, columns in self._cols_by_table.items()
        }

    @HasMemoized.memoized_attribute
    def _server_default_plus_onupdate_propkeys(self):
        result = set()

        for table, columns in self._cols_by_table.items():
            for col in columns:
                if (
                    col.server_default is not None
                    or col.server_onupdate is not None
                ) and col in self._columntoproperty:
                    result.add(self._columntoproperty[col].key)

        return result

    @HasMemoized.memoized_attribute
    def _server_onupdate_default_cols(self):
        return {
            table: frozenset(
                col.key for col in columns if col.server_onupdate is not None
            )
            for table, columns in self._cols_by_table.items()
        }

    @HasMemoized.memoized_instancemethod
    def __clause_element__(self):

        annotations = {
            "entity_namespace": self,
            "parententity": self,
            "parentmapper": self,
        }
        if self.persist_selectable is not self.local_table:
            # joined table inheritance, with polymorphic selectable,
            # etc.
            annotations["dml_table"] = self.local_table._annotate(
                {
                    "entity_namespace": self,
                    "parententity": self,
                    "parentmapper": self,
                }
            )._set_propagate_attrs(
                {"compile_state_plugin": "orm", "plugin_subject": self}
            )

        return self.selectable._annotate(annotations)._set_propagate_attrs(
            {"compile_state_plugin": "orm", "plugin_subject": self}
        )

    @util.memoized_property
    def select_identity_token(self):
        return (
            expression.null()
            ._annotate(
                {
                    "entity_namespace": self,
                    "parententity": self,
                    "parentmapper": self,
                    "identity_token": True,
                }
            )
            ._set_propagate_attrs(
                {"compile_state_plugin": "orm", "plugin_subject": self}
            )
        )

    @property
    def selectable(self):
        """The :class:`_schema.FromClause` construct this
        :class:`_orm.Mapper` selects from by default.

        Normally, this is equivalent to :attr:`.persist_selectable`, unless
        the ``with_polymorphic`` feature is in use, in which case the
        full "polymorphic" selectable is returned.

        """
        return self._with_polymorphic_selectable

    def _with_polymorphic_args(
        self, spec=None, selectable=False, innerjoin=False
    ):
        if selectable not in (None, False):
            selectable = coercions.expect(
                roles.StrictFromClauseRole, selectable, allow_select=True
            )

        if self.with_polymorphic:
            if not spec:
                spec = self.with_polymorphic[0]
            if selectable is False:
                selectable = self.with_polymorphic[1]
        elif selectable is False:
            selectable = None
        mappers = self._mappers_from_spec(spec, selectable)
        if selectable is not None:
            return mappers, selectable
        else:
            return mappers, self._selectable_from_mappers(mappers, innerjoin)

    @HasMemoized.memoized_attribute
    def _polymorphic_properties(self):
        return list(
            self._iterate_polymorphic_properties(
                self._with_polymorphic_mappers
            )
        )

    @property
    def _all_column_expressions(self):
        poly_properties = self._polymorphic_properties
        adapter = self._polymorphic_adapter

        return [
            adapter.columns[prop.columns[0]] if adapter else prop.columns[0]
            for prop in poly_properties
            if isinstance(prop, properties.ColumnProperty)
            and prop._renders_in_subqueries
        ]

    def _columns_plus_keys(self, polymorphic_mappers=()):
        if polymorphic_mappers:
            poly_properties = self._iterate_polymorphic_properties(
                polymorphic_mappers
            )
        else:
            poly_properties = self._polymorphic_properties

        return [
            (prop.key, prop.columns[0])
            for prop in poly_properties
            if isinstance(prop, properties.ColumnProperty)
        ]

    @HasMemoized.memoized_attribute
    def _polymorphic_adapter(self):
        if self.with_polymorphic:
            return sql_util.ColumnAdapter(
                self.selectable, equivalents=self._equivalent_columns
            )
        else:
            return None

    def _iterate_polymorphic_properties(self, mappers=None):
        """Return an iterator of MapperProperty objects which will render into
        a SELECT."""
        if mappers is None:
            mappers = self._with_polymorphic_mappers

        if not mappers:
            for c in self.iterate_properties:
                yield c
        else:
            # in the polymorphic case, filter out discriminator columns
            # from other mappers, as these are sometimes dependent on that
            # mapper's polymorphic selectable (which we don't want rendered)
            for c in util.unique_list(
                chain(
                    *[
                        list(mapper.iterate_properties)
                        for mapper in [self] + mappers
                    ]
                )
            ):
                if getattr(c, "_is_polymorphic_discriminator", False) and (
                    self.polymorphic_on is None
                    or c.columns[0] is not self.polymorphic_on
                ):
                    continue
                yield c

    @HasMemoized.memoized_attribute
    def attrs(self):
        """A namespace of all :class:`.MapperProperty` objects
        associated this mapper.

        This is an object that provides each property based on
        its key name.  For instance, the mapper for a
        ``User`` class which has ``User.name`` attribute would
        provide ``mapper.attrs.name``, which would be the
        :class:`.ColumnProperty` representing the ``name``
        column.   The namespace object can also be iterated,
        which would yield each :class:`.MapperProperty`.

        :class:`_orm.Mapper` has several pre-filtered views
        of this attribute which limit the types of properties
        returned, including :attr:`.synonyms`, :attr:`.column_attrs`,
        :attr:`.relationships`, and :attr:`.composites`.

        .. warning::

            The :attr:`_orm.Mapper.attrs` accessor namespace is an
            instance of :class:`.OrderedProperties`.  This is
            a dictionary-like object which includes a small number of
            named methods such as :meth:`.OrderedProperties.items`
            and :meth:`.OrderedProperties.values`.  When
            accessing attributes dynamically, favor using the dict-access
            scheme, e.g. ``mapper.attrs[somename]`` over
            ``getattr(mapper.attrs, somename)`` to avoid name collisions.

        .. seealso::

            :attr:`_orm.Mapper.all_orm_descriptors`

        """

        self._check_configure()
        return util.ImmutableProperties(self._props)

    @HasMemoized.memoized_attribute
    def all_orm_descriptors(self):
        """A namespace of all :class:`.InspectionAttr` attributes associated
        with the mapped class.

        These attributes are in all cases Python :term:`descriptors`
        associated with the mapped class or its superclasses.

        This namespace includes attributes that are mapped to the class
        as well as attributes declared by extension modules.
        It includes any Python descriptor type that inherits from
        :class:`.InspectionAttr`.  This includes
        :class:`.QueryableAttribute`, as well as extension types such as
        :class:`.hybrid_property`, :class:`.hybrid_method` and
        :class:`.AssociationProxy`.

        To distinguish between mapped attributes and extension attributes,
        the attribute :attr:`.InspectionAttr.extension_type` will refer
        to a constant that distinguishes between different extension types.

        The sorting of the attributes is based on the following rules:

        1. Iterate through the class and its superclasses in order from
           subclass to superclass (i.e. iterate through ``cls.__mro__``)

        2. For each class, yield the attributes in the order in which they
           appear in ``__dict__``, with the exception of those in step
           3 below.  In Python 3.6 and above this ordering will be the
           same as that of the class' construction, with the exception
           of attributes that were added after the fact by the application
           or the mapper.

        3. If a certain attribute key is also in the superclass ``__dict__``,
           then it's included in the iteration for that class, and not the
           class in which it first appeared.

        The above process produces an ordering that is deterministic in terms
        of the order in which attributes were assigned to the class.

        .. versionchanged:: 1.3.19 ensured deterministic ordering for
           :meth:`_orm.Mapper.all_orm_descriptors`.

        When dealing with a :class:`.QueryableAttribute`, the
        :attr:`.QueryableAttribute.property` attribute refers to the
        :class:`.MapperProperty` property, which is what you get when
        referring to the collection of mapped properties via
        :attr:`_orm.Mapper.attrs`.

        .. warning::

            The :attr:`_orm.Mapper.all_orm_descriptors`
            accessor namespace is an
            instance of :class:`.OrderedProperties`.  This is
            a dictionary-like object which includes a small number of
            named methods such as :meth:`.OrderedProperties.items`
            and :meth:`.OrderedProperties.values`.  When
            accessing attributes dynamically, favor using the dict-access
            scheme, e.g. ``mapper.all_orm_descriptors[somename]`` over
            ``getattr(mapper.all_orm_descriptors, somename)`` to avoid name
            collisions.

        .. seealso::

            :attr:`_orm.Mapper.attrs`

        """
        return util.ImmutableProperties(
            dict(self.class_manager._all_sqla_attributes())
        )

    @HasMemoized.memoized_attribute
    @util.preload_module("sqlalchemy.orm.descriptor_props")
    def synonyms(self):
        """Return a namespace of all :class:`.SynonymProperty`
        properties maintained by this :class:`_orm.Mapper`.

        .. seealso::

            :attr:`_orm.Mapper.attrs` - namespace of all
            :class:`.MapperProperty`
            objects.

        """
        descriptor_props = util.preloaded.orm_descriptor_props

        return self._filter_properties(descriptor_props.SynonymProperty)

    @property
    def entity_namespace(self):
        return self.class_

    @HasMemoized.memoized_attribute
    def column_attrs(self):
        """Return a namespace of all :class:`.ColumnProperty`
        properties maintained by this :class:`_orm.Mapper`.

        .. seealso::

            :attr:`_orm.Mapper.attrs` - namespace of all
            :class:`.MapperProperty`
            objects.

        """
        return self._filter_properties(properties.ColumnProperty)

    @util.preload_module("sqlalchemy.orm.relationships")
    @HasMemoized.memoized_attribute
    def relationships(self):
        """A namespace of all :class:`.RelationshipProperty` properties
        maintained by this :class:`_orm.Mapper`.

        .. warning::

            the :attr:`_orm.Mapper.relationships` accessor namespace is an
            instance of :class:`.OrderedProperties`.  This is
            a dictionary-like object which includes a small number of
            named methods such as :meth:`.OrderedProperties.items`
            and :meth:`.OrderedProperties.values`.  When
            accessing attributes dynamically, favor using the dict-access
            scheme, e.g. ``mapper.relationships[somename]`` over
            ``getattr(mapper.relationships, somename)`` to avoid name
            collisions.

        .. seealso::

            :attr:`_orm.Mapper.attrs` - namespace of all
            :class:`.MapperProperty`
            objects.

        """
        return self._filter_properties(
            util.preloaded.orm_relationships.RelationshipProperty
        )

    @HasMemoized.memoized_attribute
    @util.preload_module("sqlalchemy.orm.descriptor_props")
    def composites(self):
        """Return a namespace of all :class:`.CompositeProperty`
        properties maintained by this :class:`_orm.Mapper`.

        .. seealso::

            :attr:`_orm.Mapper.attrs` - namespace of all
            :class:`.MapperProperty`
            objects.

        """
        return self._filter_properties(
            util.preloaded.orm_descriptor_props.CompositeProperty
        )

    def _filter_properties(self, type_):
        self._check_configure()
        return util.ImmutableProperties(
            util.OrderedDict(
                (k, v) for k, v in self._props.items() if isinstance(v, type_)
            )
        )

    @HasMemoized.memoized_attribute
    def _get_clause(self):
        """create a "get clause" based on the primary key.  this is used
        by query.get() and many-to-one lazyloads to load this item
        by primary key.

        """
        params = [
            (
                primary_key,
                sql.bindparam("pk_%d" % idx, type_=primary_key.type),
            )
            for idx, primary_key in enumerate(self.primary_key, 1)
        ]
        return (
            sql.and_(*[k == v for (k, v) in params]),
            util.column_dict(params),
        )

    @HasMemoized.memoized_attribute
    def _equivalent_columns(self):
        """Create a map of all equivalent columns, based on
        the determination of column pairs that are equated to
        one another based on inherit condition.  This is designed
        to work with the queries that util.polymorphic_union
        comes up with, which often don't include the columns from
        the base table directly (including the subclass table columns
        only).

        The resulting structure is a dictionary of columns mapped
        to lists of equivalent columns, e.g.::

            {
                tablea.col1:
                    {tableb.col1, tablec.col1},
                tablea.col2:
                    {tabled.col2}
            }

        """
        result = util.column_dict()

        def visit_binary(binary):
            if binary.operator == operators.eq:
                if binary.left in result:
                    result[binary.left].add(binary.right)
                else:
                    result[binary.left] = util.column_set((binary.right,))
                if binary.right in result:
                    result[binary.right].add(binary.left)
                else:
                    result[binary.right] = util.column_set((binary.left,))

        for mapper in self.base_mapper.self_and_descendants:
            if mapper.inherit_condition is not None:
                visitors.traverse(
                    mapper.inherit_condition, {}, {"binary": visit_binary}
                )

        return result

    def _is_userland_descriptor(self, assigned_name, obj):
        if isinstance(
            obj,
            (
                _MappedAttribute,
                instrumentation.ClassManager,
                expression.ColumnElement,
            ),
        ):
            return False
        else:
            return assigned_name not in self._dataclass_fields

    @HasMemoized.memoized_attribute
    def _dataclass_fields(self):
        return [f.name for f in util.dataclass_fields(self.class_)]

    def _should_exclude(self, name, assigned_name, local, column):
        """determine whether a particular property should be implicitly
        present on the class.

        This occurs when properties are propagated from an inherited class, or
        are applied from the columns present in the mapped table.

        """

        # check for class-bound attributes and/or descriptors,
        # either local or from an inherited class
        # ignore dataclass field default values
        if local:
            if self.class_.__dict__.get(
                assigned_name, None
            ) is not None and self._is_userland_descriptor(
                assigned_name, self.class_.__dict__[assigned_name]
            ):
                return True
        else:
            attr = self.class_manager._get_class_attr_mro(assigned_name, None)
            if attr is not None and self._is_userland_descriptor(
                assigned_name, attr
            ):
                return True

        if (
            self.include_properties is not None
            and name not in self.include_properties
            and (column is None or column not in self.include_properties)
        ):
            self._log("not including property %s" % (name))
            return True

        if self.exclude_properties is not None and (
            name in self.exclude_properties
            or (column is not None and column in self.exclude_properties)
        ):
            self._log("excluding property %s" % (name))
            return True

        return False

    def common_parent(self, other):
        """Return true if the given mapper shares a
        common inherited parent as this mapper."""

        return self.base_mapper is other.base_mapper

    def is_sibling(self, other):
        """return true if the other mapper is an inheriting sibling to this
        one.  common parent but different branch

        """
        return (
            self.base_mapper is other.base_mapper
            and not self.isa(other)
            and not other.isa(self)
        )

    def _canload(self, state, allow_subtypes):
        s = self.primary_mapper()
        if self.polymorphic_on is not None or allow_subtypes:
            return _state_mapper(state).isa(s)
        else:
            return _state_mapper(state) is s

    def isa(self, other):
        """Return True if the this mapper inherits from the given mapper."""

        m = self
        while m and m is not other:
            m = m.inherits
        return bool(m)

    def iterate_to_root(self):
        m = self
        while m:
            yield m
            m = m.inherits

    @HasMemoized.memoized_attribute
    def self_and_descendants(self):
        """The collection including this mapper and all descendant mappers.

        This includes not just the immediately inheriting mappers but
        all their inheriting mappers as well.

        """
        descendants = []
        stack = deque([self])
        while stack:
            item = stack.popleft()
            descendants.append(item)
            stack.extend(item._inheriting_mappers)
        return util.WeakSequence(descendants)

    def polymorphic_iterator(self):
        """Iterate through the collection including this mapper and
        all descendant mappers.

        This includes not just the immediately inheriting mappers but
        all their inheriting mappers as well.

        To iterate through an entire hierarchy, use
        ``mapper.base_mapper.polymorphic_iterator()``.

        """
        return iter(self.self_and_descendants)

    def primary_mapper(self):
        """Return the primary mapper corresponding to this mapper's class key
        (class)."""

        return self.class_manager.mapper

    @property
    def primary_base_mapper(self):
        return self.class_manager.mapper.base_mapper

    def _result_has_identity_key(self, result, adapter=None):
        pk_cols = self.primary_key
        if adapter:
            pk_cols = [adapter.columns[c] for c in pk_cols]
        rk = result.keys()
        for col in pk_cols:
            if col not in rk:
                return False
        else:
            return True

    def identity_key_from_row(self, row, identity_token=None, adapter=None):
        """Return an identity-map key for use in storing/retrieving an
        item from the identity map.

        :param row: A :class:`.Row` instance.  The columns which are
         mapped by this :class:`_orm.Mapper` should be locatable in the row,
         preferably via the :class:`_schema.Column`
         object directly (as is the case
         when a :func:`_expression.select` construct is executed), or
         via string names of the form ``<tablename>_<colname>``.

        """
        pk_cols = self.primary_key
        if adapter:
            pk_cols = [adapter.columns[c] for c in pk_cols]

        return (
            self._identity_class,
            tuple(row[column] for column in pk_cols),
            identity_token,
        )

    def identity_key_from_primary_key(self, primary_key, identity_token=None):
        """Return an identity-map key for use in storing/retrieving an
        item from an identity map.

        :param primary_key: A list of values indicating the identifier.

        """
        return self._identity_class, tuple(primary_key), identity_token

    def identity_key_from_instance(self, instance):
        """Return the identity key for the given instance, based on
        its primary key attributes.

        If the instance's state is expired, calling this method
        will result in a database check to see if the object has been deleted.
        If the row no longer exists,
        :class:`~sqlalchemy.orm.exc.ObjectDeletedError` is raised.

        This value is typically also found on the instance state under the
        attribute name `key`.

        """
        state = attributes.instance_state(instance)
        return self._identity_key_from_state(state, attributes.PASSIVE_OFF)

    def _identity_key_from_state(
        self, state, passive=attributes.PASSIVE_RETURN_NO_VALUE
    ):
        dict_ = state.dict
        manager = state.manager
        return (
            self._identity_class,
            tuple(
                manager[prop.key].impl.get(state, dict_, passive)
                for prop in self._identity_key_props
            ),
            state.identity_token,
        )

    def primary_key_from_instance(self, instance):
        """Return the list of primary key values for the given
        instance.

        If the instance's state is expired, calling this method
        will result in a database check to see if the object has been deleted.
        If the row no longer exists,
        :class:`~sqlalchemy.orm.exc.ObjectDeletedError` is raised.

        """
        state = attributes.instance_state(instance)
        identity_key = self._identity_key_from_state(
            state, attributes.PASSIVE_OFF
        )
        return identity_key[1]

    @HasMemoized.memoized_attribute
    def _persistent_sortkey_fn(self):
        key_fns = [col.type.sort_key_function for col in self.primary_key]

        if set(key_fns).difference([None]):

            def key(state):
                return tuple(
                    key_fn(val) if key_fn is not None else val
                    for key_fn, val in zip(key_fns, state.key[1])
                )

        else:

            def key(state):
                return state.key[1]

        return key

    @HasMemoized.memoized_attribute
    def _identity_key_props(self):
        return [self._columntoproperty[col] for col in self.primary_key]

    @HasMemoized.memoized_attribute
    def _all_pk_cols(self):
        collection = set()
        for table in self.tables:
            collection.update(self._pks_by_table[table])
        return collection

    @HasMemoized.memoized_attribute
    def _should_undefer_in_wildcard(self):
        cols = set(self.primary_key)
        if self.polymorphic_on is not None:
            cols.add(self.polymorphic_on)
        return cols

    @HasMemoized.memoized_attribute
    def _primary_key_propkeys(self):
        return {self._columntoproperty[col].key for col in self._all_pk_cols}

    def _get_state_attr_by_column(
        self, state, dict_, column, passive=attributes.PASSIVE_RETURN_NO_VALUE
    ):
        prop = self._columntoproperty[column]
        return state.manager[prop.key].impl.get(state, dict_, passive=passive)

    def _set_committed_state_attr_by_column(self, state, dict_, column, value):
        prop = self._columntoproperty[column]
        state.manager[prop.key].impl.set_committed_value(state, dict_, value)

    def _set_state_attr_by_column(self, state, dict_, column, value):
        prop = self._columntoproperty[column]
        state.manager[prop.key].impl.set(state, dict_, value, None)

    def _get_committed_attr_by_column(self, obj, column):
        state = attributes.instance_state(obj)
        dict_ = attributes.instance_dict(obj)
        return self._get_committed_state_attr_by_column(
            state, dict_, column, passive=attributes.PASSIVE_OFF
        )

    def _get_committed_state_attr_by_column(
        self, state, dict_, column, passive=attributes.PASSIVE_RETURN_NO_VALUE
    ):

        prop = self._columntoproperty[column]
        return state.manager[prop.key].impl.get_committed_value(
            state, dict_, passive=passive
        )

    def _optimized_get_statement(self, state, attribute_names):
        """assemble a WHERE clause which retrieves a given state by primary
        key, using a minimized set of tables.

        Applies to a joined-table inheritance mapper where the
        requested attribute names are only present on joined tables,
        not the base table.  The WHERE clause attempts to include
        only those tables to minimize joins.

        """
        props = self._props

        col_attribute_names = set(attribute_names).intersection(
            state.mapper.column_attrs.keys()
        )
        tables = set(
            chain(
                *[
                    sql_util.find_tables(c, check_columns=True)
                    for key in col_attribute_names
                    for c in props[key].columns
                ]
            )
        )

        if self.base_mapper.local_table in tables:
            return None

        def visit_binary(binary):
            leftcol = binary.left
            rightcol = binary.right
            if leftcol is None or rightcol is None:
                return

            if leftcol.table not in tables:
                leftval = self._get_committed_state_attr_by_column(
                    state,
                    state.dict,
                    leftcol,
                    passive=attributes.PASSIVE_NO_INITIALIZE,
                )
                if leftval in orm_util._none_set:
                    raise _OptGetColumnsNotAvailable()
                binary.left = sql.bindparam(
                    None, leftval, type_=binary.right.type
                )
            elif rightcol.table not in tables:
                rightval = self._get_committed_state_attr_by_column(
                    state,
                    state.dict,
                    rightcol,
                    passive=attributes.PASSIVE_NO_INITIALIZE,
                )
                if rightval in orm_util._none_set:
                    raise _OptGetColumnsNotAvailable()
                binary.right = sql.bindparam(
                    None, rightval, type_=binary.right.type
                )

        allconds = []

        start = False

        # as of #7507, from the lowest base table on upwards,
        # we include all intermediary tables.

        for mapper in reversed(list(self.iterate_to_root())):
            if mapper.local_table in tables:
                start = True
            elif not isinstance(mapper.local_table, expression.TableClause):
                return None
            if start and not mapper.single:
                allconds.append(mapper.inherit_condition)
                tables.add(mapper.local_table)

        # only the bottom table needs its criteria to be altered to fit
        # the primary key ident - the rest of the tables upwards to the
        # descendant-most class should all be present and joined to each
        # other.
        try:
            allconds[0] = visitors.cloned_traverse(
                allconds[0], {}, {"binary": visit_binary}
            )
        except _OptGetColumnsNotAvailable:
            return None

        cond = sql.and_(*allconds)

        cols = []
        for key in col_attribute_names:
            cols.extend(props[key].columns)
        return (
            sql.select(*cols)
            .where(cond)
            .set_label_style(LABEL_STYLE_TABLENAME_PLUS_COL)
        )

    def _iterate_to_target_viawpoly(self, mapper):
        if self.isa(mapper):
            prev = self
            for m in self.iterate_to_root():
                yield m

                if m is not prev and prev not in m._with_polymorphic_mappers:
                    break

                prev = m
                if m is mapper:
                    break

    def _should_selectin_load(self, enabled_via_opt, polymorphic_from):
        if not enabled_via_opt:
            # common case, takes place for all polymorphic loads
            mapper = polymorphic_from
            for m in self._iterate_to_target_viawpoly(mapper):
                if m.polymorphic_load == "selectin":
                    return m
        else:
            # uncommon case, selectin load options were used
            enabled_via_opt = set(enabled_via_opt)
            enabled_via_opt_mappers = {e.mapper: e for e in enabled_via_opt}
            for entity in enabled_via_opt.union([polymorphic_from]):
                mapper = entity.mapper
                for m in self._iterate_to_target_viawpoly(mapper):
                    if (
                        m.polymorphic_load == "selectin"
                        or m in enabled_via_opt_mappers
                    ):
                        return enabled_via_opt_mappers.get(m, m)

        return None

    @util.preload_module("sqlalchemy.orm.strategy_options")
    def _subclass_load_via_in(self, entity):
        """Assemble a that can load the columns local to
        this subclass as a SELECT with IN.

        """
        strategy_options = util.preloaded.orm_strategy_options

        assert self.inherits

        polymorphic_prop = self._columntoproperty[self.polymorphic_on]
        keep_props = set([polymorphic_prop] + self._identity_key_props)

        disable_opt = strategy_options.Load(entity)
        enable_opt = strategy_options.Load(entity)

        for prop in self.attrs:
            if prop.parent is self or prop in keep_props:
                # "enable" options, to turn on the properties that we want to
                # load by default (subject to options from the query)
                enable_opt = enable_opt._set_generic_strategy(
                    # convert string name to an attribute before passing
                    # to loader strategy
                    (getattr(entity.entity_namespace, prop.key),),
                    dict(prop.strategy_key),
                    _reconcile_to_other=True,
                )
            else:
                # "disable" options, to turn off the properties from the
                # superclass that we *don't* want to load, applied after
                # the options from the query to override them
                disable_opt = disable_opt._set_generic_strategy(
                    # convert string name to an attribute before passing
                    # to loader strategy
                    (getattr(entity.entity_namespace, prop.key),),
                    {"do_nothing": True},
                    _reconcile_to_other=False,
                )

        primary_key = [
            sql_util._deep_annotate(pk, {"_orm_adapt": True})
            for pk in self.primary_key
        ]

        if len(primary_key) > 1:
            in_expr = sql.tuple_(*primary_key)
        else:
            in_expr = primary_key[0]

        if entity.is_aliased_class:
            assert entity.mapper is self

            q = sql.select(entity).set_label_style(
                LABEL_STYLE_TABLENAME_PLUS_COL
            )

            in_expr = entity._adapter.traverse(in_expr)
            primary_key = [entity._adapter.traverse(k) for k in primary_key]
            q = q.where(
                in_expr.in_(sql.bindparam("primary_keys", expanding=True))
            ).order_by(*primary_key)
        else:

            q = sql.select(self).set_label_style(
                LABEL_STYLE_TABLENAME_PLUS_COL
            )
            q = q.where(
                in_expr.in_(sql.bindparam("primary_keys", expanding=True))
            ).order_by(*primary_key)

        return q, enable_opt, disable_opt

    @HasMemoized.memoized_attribute
    def _subclass_load_via_in_mapper(self):
        return self._subclass_load_via_in(self)

    def cascade_iterator(self, type_, state, halt_on=None):
        r"""Iterate each element and its mapper in an object graph,
        for all relationships that meet the given cascade rule.

        :param type\_:
          The name of the cascade rule (i.e. ``"save-update"``, ``"delete"``,
          etc.).

          .. note::  the ``"all"`` cascade is not accepted here.  For a generic
             object traversal function, see :ref:`faq_walk_objects`.

        :param state:
          The lead InstanceState.  child items will be processed per
          the relationships defined for this object's mapper.

        :return: the method yields individual object instances.

        .. seealso::

            :ref:`unitofwork_cascades`

            :ref:`faq_walk_objects` - illustrates a generic function to
            traverse all objects without relying on cascades.

        """
        visited_states = set()
        prp, mpp = object(), object()

        assert state.mapper.isa(self)

        visitables = deque(
            [(deque(state.mapper._props.values()), prp, state, state.dict)]
        )

        while visitables:
            iterator, item_type, parent_state, parent_dict = visitables[-1]
            if not iterator:
                visitables.pop()
                continue

            if item_type is prp:
                prop = iterator.popleft()
                if type_ not in prop.cascade:
                    continue
                queue = deque(
                    prop.cascade_iterator(
                        type_,
                        parent_state,
                        parent_dict,
                        visited_states,
                        halt_on,
                    )
                )
                if queue:
                    visitables.append((queue, mpp, None, None))
            elif item_type is mpp:
                (
                    instance,
                    instance_mapper,
                    corresponding_state,
                    corresponding_dict,
                ) = iterator.popleft()
                yield (
                    instance,
                    instance_mapper,
                    corresponding_state,
                    corresponding_dict,
                )
                visitables.append(
                    (
                        deque(instance_mapper._props.values()),
                        prp,
                        corresponding_state,
                        corresponding_dict,
                    )
                )

    @HasMemoized.memoized_attribute
    def _compiled_cache(self):
        return util.LRUCache(self._compiled_cache_size)

    @HasMemoized.memoized_attribute
    def _sorted_tables(self):
        table_to_mapper = {}

        for mapper in self.base_mapper.self_and_descendants:
            for t in mapper.tables:
                table_to_mapper.setdefault(t, mapper)

        extra_dependencies = []
        for table, mapper in table_to_mapper.items():
            super_ = mapper.inherits
            if super_:
                extra_dependencies.extend(
                    [(super_table, table) for super_table in super_.tables]
                )

        def skip(fk):
            # attempt to skip dependencies that are not
            # significant to the inheritance chain
            # for two tables that are related by inheritance.
            # while that dependency may be important, it's technically
            # not what we mean to sort on here.
            parent = table_to_mapper.get(fk.parent.table)
            dep = table_to_mapper.get(fk.column.table)
            if (
                parent is not None
                and dep is not None
                and dep is not parent
                and dep.inherit_condition is not None
            ):
                cols = set(sql_util._find_columns(dep.inherit_condition))
                if parent.inherit_condition is not None:
                    cols = cols.union(
                        sql_util._find_columns(parent.inherit_condition)
                    )
                    return fk.parent not in cols and fk.column not in cols
                else:
                    return fk.parent not in cols
            return False

        sorted_ = sql_util.sort_tables(
            table_to_mapper,
            skip_fn=skip,
            extra_dependencies=extra_dependencies,
        )

        ret = util.OrderedDict()
        for t in sorted_:
            ret[t] = table_to_mapper[t]
        return ret

    def _memo(self, key, callable_):
        if key in self._memoized_values:
            return self._memoized_values[key]
        else:
            self._memoized_values[key] = value = callable_()
            return value

    @util.memoized_property
    def _table_to_equated(self):
        """memoized map of tables to collections of columns to be
        synchronized upwards to the base mapper."""

        result = util.defaultdict(list)

        for table in self._sorted_tables:
            cols = set(table.c)
            for m in self.iterate_to_root():
                if m._inherits_equated_pairs and cols.intersection(
                    reduce(
                        set.union,
                        [l.proxy_set for l, r in m._inherits_equated_pairs],
                    )
                ):
                    result[table].append((m, m._inherits_equated_pairs))

        return result


class _OptGetColumnsNotAvailable(Exception):
    pass


def configure_mappers():
    """Initialize the inter-mapper relationships of all mappers that
    have been constructed thus far across all :class:`_orm.registry`
    collections.

    The configure step is used to reconcile and initialize the
    :func:`_orm.relationship` linkages between mapped classes, as well as to
    invoke configuration events such as the
    :meth:`_orm.MapperEvents.before_configured` and
    :meth:`_orm.MapperEvents.after_configured`, which may be used by ORM
    extensions or user-defined extension hooks.

    Mapper configuration is normally invoked automatically, the first time
    mappings from a particular :class:`_orm.registry` are used, as well as
    whenever mappings are used and additional not-yet-configured mappers have
    been constructed. The automatic configuration process however is local only
    to the :class:`_orm.registry` involving the target mapper and any related
    :class:`_orm.registry` objects which it may depend on; this is
    equivalent to invoking the :meth:`_orm.registry.configure` method
    on a particular :class:`_orm.registry`.

    By contrast, the :func:`_orm.configure_mappers` function will invoke the
    configuration process on all :class:`_orm.registry` objects that
    exist in memory, and may be useful for scenarios where many individual
    :class:`_orm.registry` objects that are nonetheless interrelated are
    in use.

    .. versionchanged:: 1.4

        As of SQLAlchemy 1.4.0b2, this function works on a
        per-:class:`_orm.registry` basis, locating all :class:`_orm.registry`
        objects present and invoking the :meth:`_orm.registry.configure` method
        on each. The :meth:`_orm.registry.configure` method may be preferred to
        limit the configuration of mappers to those local to a particular
        :class:`_orm.registry` and/or declarative base class.

    Points at which automatic configuration is invoked include when a mapped
    class is instantiated into an instance, as well as when ORM queries
    are emitted using :meth:`.Session.query` or :meth:`_orm.Session.execute`
    with an ORM-enabled statement.

    The mapper configure process, whether invoked by
    :func:`_orm.configure_mappers` or from :meth:`_orm.registry.configure`,
    provides several event hooks that can be used to augment the mapper
    configuration step. These hooks include:

    * :meth:`.MapperEvents.before_configured` - called once before
      :func:`.configure_mappers` or :meth:`_orm.registry.configure` does any
      work; this can be used to establish additional options, properties, or
      related mappings before the operation proceeds.

    * :meth:`.MapperEvents.mapper_configured` - called as each individual
      :class:`_orm.Mapper` is configured within the process; will include all
      mapper state except for backrefs set up by other mappers that are still
      to be configured.

    * :meth:`.MapperEvents.after_configured` - called once after
      :func:`.configure_mappers` or :meth:`_orm.registry.configure` is
      complete; at this stage, all :class:`_orm.Mapper` objects that fall
      within the scope of the configuration operation will be fully configured.
      Note that the calling application may still have other mappings that
      haven't been produced yet, such as if they are in modules as yet
      unimported, and may also have mappings that are still to be configured,
      if they are in other :class:`_orm.registry` collections not part of the
      current scope of configuration.

    """

    _configure_registries(_all_registries(), cascade=True)


def _configure_registries(registries, cascade):
    for reg in registries:
        if reg._new_mappers:
            break
    else:
        return

    with _CONFIGURE_MUTEX:
        global _already_compiling
        if _already_compiling:
            return
        _already_compiling = True
        try:

            # double-check inside mutex
            for reg in registries:
                if reg._new_mappers:
                    break
            else:
                return

            Mapper.dispatch._for_class(Mapper).before_configured()
            # initialize properties on all mappers
            # note that _mapper_registry is unordered, which
            # may randomly conceal/reveal issues related to
            # the order of mapper compilation

            _do_configure_registries(registries, cascade)
        finally:
            _already_compiling = False
    Mapper.dispatch._for_class(Mapper).after_configured()


@util.preload_module("sqlalchemy.orm.decl_api")
def _do_configure_registries(registries, cascade):

    registry = util.preloaded.orm_decl_api.registry

    orig = set(registries)

    for reg in registry._recurse_with_dependencies(registries):
        has_skip = False

        for mapper in reg._mappers_to_configure():
            run_configure = None
            for fn in mapper.dispatch.before_mapper_configured:
                run_configure = fn(mapper, mapper.class_)
                if run_configure is EXT_SKIP:
                    has_skip = True
                    break
            if run_configure is EXT_SKIP:
                continue

            if getattr(mapper, "_configure_failed", False):
                e = sa_exc.InvalidRequestError(
                    "One or more mappers failed to initialize - "
                    "can't proceed with initialization of other "
                    "mappers. Triggering mapper: '%s'. "
                    "Original exception was: %s"
                    % (mapper, mapper._configure_failed)
                )
                e._configure_failed = mapper._configure_failed
                raise e

            if not mapper.configured:
                try:
                    mapper._post_configure_properties()
                    mapper._expire_memoizations()
                    mapper.dispatch.mapper_configured(mapper, mapper.class_)
                except Exception:
                    exc = sys.exc_info()[1]
                    if not hasattr(exc, "_configure_failed"):
                        mapper._configure_failed = exc
                    raise
        if not has_skip:
            reg._new_mappers = False

        if not cascade and reg._dependencies.difference(orig):
            raise sa_exc.InvalidRequestError(
                "configure was called with cascade=False but "
                "additional registries remain"
            )


@util.preload_module("sqlalchemy.orm.decl_api")
def _dispose_registries(registries, cascade):

    registry = util.preloaded.orm_decl_api.registry

    orig = set(registries)

    for reg in registry._recurse_with_dependents(registries):
        if not cascade and reg._dependents.difference(orig):
            raise sa_exc.InvalidRequestError(
                "Registry has dependent registries that are not disposed; "
                "pass cascade=True to clear these also"
            )

        while reg._managers:
            try:
                manager, _ = reg._managers.popitem()
            except KeyError:
                # guard against race between while and popitem
                pass
            else:
                reg._dispose_manager_and_mapper(manager)

        reg._non_primary_mappers.clear()
        reg._dependents.clear()
        for dep in reg._dependencies:
            dep._dependents.discard(reg)
        reg._dependencies.clear()
        # this wasn't done in the 1.3 clear_mappers() and in fact it
        # was a bug, as it could cause configure_mappers() to invoke
        # the "before_configured" event even though mappers had all been
        # disposed.
        reg._new_mappers = False


def reconstructor(fn):
    """Decorate a method as the 'reconstructor' hook.

    Designates a single method as the "reconstructor", an ``__init__``-like
    method that will be called by the ORM after the instance has been
    loaded from the database or otherwise reconstituted.

    The reconstructor will be invoked with no arguments.  Scalar
    (non-collection) database-mapped attributes of the instance will
    be available for use within the function.  Eagerly-loaded
    collections are generally not yet available and will usually only
    contain the first element.  ORM state changes made to objects at
    this stage will not be recorded for the next flush() operation, so
    the activity within a reconstructor should be conservative.

    .. seealso::

        :ref:`mapping_constructors`

        :meth:`.InstanceEvents.load`

    """
    fn.__sa_reconstructor__ = True
    return fn


def validates(*names, **kw):
    r"""Decorate a method as a 'validator' for one or more named properties.

    Designates a method as a validator, a method which receives the
    name of the attribute as well as a value to be assigned, or in the
    case of a collection, the value to be added to the collection.
    The function can then raise validation exceptions to halt the
    process from continuing (where Python's built-in ``ValueError``
    and ``AssertionError`` exceptions are reasonable choices), or can
    modify or replace the value before proceeding. The function should
    otherwise return the given value.

    Note that a validator for a collection **cannot** issue a load of that
    collection within the validation routine - this usage raises
    an assertion to avoid recursion overflows.  This is a reentrant
    condition which is not supported.

    :param \*names: list of attribute names to be validated.
    :param include_removes: if True, "remove" events will be
     sent as well - the validation function must accept an additional
     argument "is_remove" which will be a boolean.

    :param include_backrefs: defaults to ``True``; if ``False``, the
     validation function will not emit if the originator is an attribute
     event related via a backref.  This can be used for bi-directional
     :func:`.validates` usage where only one validator should emit per
     attribute operation.

     .. versionadded:: 0.9.0

    .. seealso::

      :ref:`simple_validators` - usage examples for :func:`.validates`

    """
    include_removes = kw.pop("include_removes", False)
    include_backrefs = kw.pop("include_backrefs", True)

    def wrap(fn):
        fn.__sa_validators__ = names
        fn.__sa_validation_opts__ = {
            "include_removes": include_removes,
            "include_backrefs": include_backrefs,
        }
        return fn

    return wrap


def _event_on_load(state, ctx):
    instrumenting_mapper = state.manager.mapper

    if instrumenting_mapper._reconstructor:
        instrumenting_mapper._reconstructor(state.obj())


def _event_on_init(state, args, kwargs):
    """Run init_instance hooks.

    This also includes mapper compilation, normally not needed
    here but helps with some piecemeal configuration
    scenarios (such as in the ORM tutorial).

    """

    instrumenting_mapper = state.manager.mapper
    if instrumenting_mapper:
        instrumenting_mapper._check_configure()
        if instrumenting_mapper._set_polymorphic_identity:
            instrumenting_mapper._set_polymorphic_identity(state)


class _ColumnMapping(dict):
    """Error reporting helper for mapper._columntoproperty."""

    __slots__ = ("mapper",)

    def __init__(self, mapper):
        self.mapper = mapper

    def __missing__(self, column):
        prop = self.mapper._props.get(column)
        if prop:
            raise orm_exc.UnmappedColumnError(
                "Column '%s.%s' is not available, due to "
                "conflicting property '%s':%r"
                % (column.table.name, column.name, column.key, prop)
            )
        raise orm_exc.UnmappedColumnError(
            "No column %s is configured on mapper %s..."
            % (column, self.mapper)
        )
