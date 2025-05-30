# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for
# license information.
# --------------------------------------------------------------------------
"""Query Provider additional connection methods."""
from __future__ import annotations

import asyncio
import logging
from abc import abstractmethod
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from itertools import tee
from typing import TYPE_CHECKING, Any, Protocol

import nest_asyncio
import pandas as pd
from tqdm.auto import tqdm
from typing_extensions import Self

from ..._version import VERSION
from ...common.exceptions import MsticpyDataQueryError
from ...common.utility.ipython import is_ipython
from ..drivers.driver_base import DriverBase, DriverProps

if TYPE_CHECKING:
    from datetime import datetime

    from .query_source import QuerySource

__version__ = VERSION
__author__ = "Ian Hellen"

logger: logging.Logger = logging.getLogger(__name__)


# pylint: disable=too-few-public-methods, unnecessary-ellipsis
class QueryProviderProtocol(Protocol):
    """Protocol for required properties of QueryProvider class."""

    driver_class: type[DriverBase]
    _driver_kwargs: dict[str, Any]
    _additional_connections: dict[str, DriverBase]
    _query_provider: DriverBase

    @staticmethod
    @abstractmethod
    def _get_query_options(
        params: dict[str, Any],
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        """Return any kwargs not already in params."""


# pylint: disable=super-init-not-called
class QueryProviderConnectionsMixin(QueryProviderProtocol):
    """Mixin additional connection handling QueryProvider class."""

    @staticmethod
    @abstractmethod
    def _get_query_options(
        params: dict[str, Any],
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        """Return any kwargs not already in params."""

    def exec_query(self: Self, query: str, **kwargs) -> pd.DataFrame | str | None:
        """
        Execute simple query string.

        Parameters
        ----------
        query : str
            [description]
        use_connections : Union[str, list[str]]

        Other Parameters
        ----------------
        query_options : dict[str, Any]
            Additional options passed to query driver.
        kwargs : dict[str, Any]
            Additional options passed to query driver.

        Returns
        -------
        Union[pd.DataFrame, Any]
            Query results - a DataFrame if successful
            or a KqlResult if unsuccessful.

        """
        query_options: dict[str, Any] = kwargs.pop("query_options", {}) or kwargs
        query_source: QuerySource | None = kwargs.pop("query_source", None)

        logger.info("Executing query '%s...'", query[:40])
        logger.debug("Full query: %s", query)
        logger.debug("Query options: %s", query_options)
        if not self._additional_connections:
            return self._query_provider.query(
                query,
                query_source=query_source,
                **query_options,
            )
        return self._exec_additional_connections(query, **kwargs)

    def add_connection(
        self: Self,
        connection_str: str | None = None,
        alias: str | None = None,
        **kwargs,
    ) -> None:
        """
        Add an additional connection for the query provider.

        Parameters
        ----------
        connection_str : Optional[str], optional
            Connection string for the provider, by default None
        alias : Optional[str], optional
            Alias to use for the connection, by default None

        Other Parameters
        ----------------
        kwargs : dict[str, Any]
            Other connection parameters passed to the driver.

        Notes
        -----
        Some drivers may accept types other than strings for the
        `connection_str` parameter.

        """
        # create a new instance of the driver class
        new_driver: DriverBase = self.driver_class(**(self._driver_kwargs))
        # connect
        new_driver.connect(connection_str=connection_str, **kwargs)
        # add to collection
        driver_key: str = alias or str(len(self._additional_connections))
        self._additional_connections[driver_key] = new_driver

    def list_connections(self: Self) -> list[str]:
        """
        Return a list of current connections.

        Returns
        -------
        list[str]
            The alias and connection string for each connection.

        """
        add_connections: list[str] = [
            f"{alias}: {driver.current_connection}"
            for alias, driver in self._additional_connections.items()
        ]
        return [f"Default: {self._query_provider.current_connection}", *add_connections]

    # pylint: disable=too-many-locals
    def _exec_additional_connections(
        self: Self,
        query: str,
        *,
        progress: bool = True,
        retry_on_error: bool = False,
        **kwargs,
    ) -> pd.DataFrame:
        """
        Return results of query run query against additional connections.

        Parameters
        ----------
        query : str
            The query to execute.
        progress: bool, optional
            Show progress bar, by default True
        retry_on_error: bool, optional
            Retry failed queries, by default False
        **kwargs : dict[str, Any]
            Additional keyword arguments to pass to the query method.

        Returns
        -------
        pd.DataFrame
            The concatenated results of the query executed against all connections.

        Notes
        -----
        This method executes the specified query against all additional connections
        added to the query provider.
        If the driver supports threading or async execution, the per-connection
        queries are executed asynchronously.
        Otherwise, the queries are executed sequentially.

        """
        # Add the initial connection
        query_tasks: dict[str, partial[pd.DataFrame | str | None]] = {
            self._query_provider.current_connection
            or "0": partial(
                self._query_provider.query,
                query,
                **kwargs,
            ),
        }
        # add the additional connections
        query_tasks.update(
            {
                name: partial(connection.query, query, **kwargs)
                for name, connection in self._additional_connections.items()
            },
        )

        logger.info("Running queries for %s connections.", len(query_tasks))
        # Run the queries threaded if supported
        if self._query_provider.get_driver_property(DriverProps.SUPPORTS_THREADING):
            logger.info("Running threaded queries.")
            event_loop: asyncio.AbstractEventLoop = _get_event_loop()
            max_workers: int = self._query_provider.get_driver_property(
                DriverProps.MAX_PARALLEL,
            )
            return event_loop.run_until_complete(
                self._exec_queries_threaded(
                    query_tasks,
                    progress=progress,
                    retry=retry_on_error,
                    max_workers=max_workers,
                ),
            )

        # standard synchronous execution
        logger.info(
            "Running query for %d connections.",
            len(self._additional_connections),
        )
        return self._exec_synchronous_queries(
            progress=progress,
            query_tasks=query_tasks,
        )

    def _exec_split_query(
        self: Self,
        split_by: str,
        query_source: QuerySource,
        query_params: dict[str, Any],
        *,
        progress: bool = True,
        retry_on_error: bool = False,
        debug: bool = False,
        **kwargs,
    ) -> pd.DataFrame | str | None:
        """
        Execute a query that is split into multiple queries.

        Parameters
        ----------
        split_by : str
            The time interval to split the query by.
        query_source : QuerySource
            The query to execute.
        query_params : dict[str, Any]
            The parameters to pass to the query.

        Other Parameters
        ----------------
        debug: bool, optional
            Return queries to be executed rather than execute them, by default False
        progress: bool, optional
            Show progress bar, by default True
        retry_on_error: bool, optional
            Retry failed queries, by default False
        **kwargs : dict[str, Any]
            Additional keyword arguments to pass to the query method.

        Returns
        -------
        pd.DataFrame
            The concatenated results of the query executed against all connections.

        Notes
        -----
        This method executes the time-chunks of the split query.
        If the driver supports threading or async execution, the sub-queries are
        executed asynchronously. Otherwise, the queries are executed sequentially.

        """
        start: datetime | None = query_params.pop("start", None)
        end: datetime | None = query_params.pop("end", None)
        if not (start and end):
            logger.warning("Cannot split a query with no 'start' and 'end' parameters")
            return None

        split_queries: dict[tuple[datetime, datetime], str] = (
            self._create_split_queries(
                query_source=query_source,
                query_params=query_params,
                start=start,
                end=end,
                split_by=split_by,
            )
        )
        if debug:
            return "\n\n".join(
                f"{start}-{end}\n{query}"
                for (start, end), query in split_queries.items()
            )

        query_tasks: dict[str, partial[pd.DataFrame | str | None]] = (
            self._create_split_query_tasks(
                query_source,
                query_params,
                split_queries,
                **kwargs,
            )
        )
        # Run the queries threaded if supported
        if self._query_provider.get_driver_property(DriverProps.SUPPORTS_THREADING):
            logger.info("Running threaded queries.")
            event_loop: asyncio.AbstractEventLoop = _get_event_loop()
            max_workers: int = self._query_provider.get_driver_property(
                DriverProps.MAX_PARALLEL,
            )
            return event_loop.run_until_complete(
                self._exec_queries_threaded(
                    query_tasks,
                    progress=progress,
                    retry=retry_on_error,
                    max_workers=max_workers,
                ),
            )

        # or revert to standard synchronous execution
        return self._exec_synchronous_queries(
            progress=progress,
            query_tasks=query_tasks,
        )

    def _create_split_query_tasks(
        self: Self,
        query_source: QuerySource,
        query_params: dict[str, Any],
        split_queries: dict[tuple[datetime, datetime], str],
        **kwargs,
    ) -> dict[str, partial[pd.DataFrame | str | None]]:
        """Return dictionary of partials to execute queries."""
        # Retrieve any query options passed (other than query params)
        query_options: dict[str, Any] = self._get_query_options(query_params, kwargs)
        logger.info("query_options: %s", query_options)
        logger.info("kwargs: %s", kwargs)
        if "time_span" in query_options:
            del query_options["time_span"]
        return {
            f"{start}-{end}": partial(
                self.exec_query,
                query=query_str,
                query_source=query_source,
                time_span={"start": start, "end": end},
                **query_options,
            )
            for (start, end), query_str in split_queries.items()
        }

    @staticmethod
    def _exec_synchronous_queries(
        *,
        progress: bool,
        query_tasks: dict[str, Any],
    ) -> pd.DataFrame:
        logger.info("Running queries sequentially.")
        results: list[pd.DataFrame] = []
        if progress:
            query_iter = tqdm(query_tasks.items(), unit="sub-queries", desc="Running")
        else:
            query_iter = query_tasks.items()
        for con_name, query_task in query_iter:
            try:
                results.append(query_task())
            except MsticpyDataQueryError:
                logger.info("Query %s failed.", con_name)
        if results:
            return pd.concat(results)

        logger.warning("All queries failed.")
        return pd.DataFrame()

    def _create_split_queries(
        self,
        query_source: QuerySource,
        query_params: dict[str, Any],
        start: datetime,
        end: datetime,
        split_by: str,
    ) -> dict[tuple[datetime, datetime], str]:
        """Return separate queries for split time ranges."""
        try:
            if split_by.strip().endswith("H"):
                split_by = split_by.replace("H", "h")
            split_delta = pd.Timedelta(split_by)
        except ValueError:
            split_delta = pd.Timedelta("1D")
        logger.info("Using split delta %s", split_delta)

        ranges: list[tuple[datetime, datetime]] = _calc_split_ranges(
            start,
            end,
            split_delta,
        )

        split_queries: dict[tuple[datetime, datetime], str] = {
            (q_start, q_end): query_source.create_query(
                formatters=self._query_provider.formatters,
                start=q_start,
                end=q_end,
                **query_params,
            )
            for q_start, q_end in ranges
        }
        logger.info("Split query into %s chunks", len(split_queries))
        return split_queries

    @staticmethod
    async def _exec_queries_threaded(
        query_tasks: dict[str, partial],
        *,
        progress: bool = True,
        retry: bool = False,
        max_workers: int = 4,
    ) -> pd.DataFrame:
        """Return results of multiple queries run as threaded tasks."""
        logger.info("Running threaded queries for %d connections.", len(query_tasks))

        event_loop: asyncio.AbstractEventLoop = _get_event_loop()

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # add the additional connections
            thread_tasks: dict[str, asyncio.Future[pd.DataFrame]] = {
                query_id: event_loop.run_in_executor(executor, query_func)
                for query_id, query_func in query_tasks.items()
            }
            results: list[pd.DataFrame] = []
            failed_tasks_ids: list[str] = []
            if progress:
                task_iter = tqdm(
                    asyncio.as_completed(thread_tasks.values()),
                    unit="sub-queries",
                    desc="Running",
                )
            else:
                task_iter = asyncio.as_completed(thread_tasks.values())
            ids_and_tasks = dict(zip(thread_tasks, task_iter))
            for query_id, thread_task in ids_and_tasks.items():
                try:
                    result: pd.DataFrame | str | None = await thread_task
                    logger.info("Query task '%s' completed successfully.", query_id)
                    results.append(result)
                except Exception:  # pylint: disable=broad-exception-caught
                    logger.warning(
                        "Query task '%s' failed with exception",
                        query_id,
                    )
                    # Reusing thread task would result in:
                    # RuntimeError: cannot reuse already awaited coroutine
                    # A new task should be queued
                    failed_tasks_ids.append(query_id)

        # Sort the results by the order of the tasks
        results = [result for _, result in sorted(zip(thread_tasks, results))]

        if retry and failed_tasks_ids:
            failed_results: pd.DataFrame = (
                await QueryProviderConnectionsMixin._exec_queries_threaded(
                    {
                        failed_tasks_id: query_tasks[failed_tasks_id]
                        for failed_tasks_id in failed_tasks_ids
                    },
                    progress=progress,
                    retry=False,
                    max_workers=max_workers,
                )
            )
            if not failed_results.empty:
                results.append(failed_results)
        if results:
            return pd.concat(results, ignore_index=True)

        logger.warning("All queries failed.")
        return pd.DataFrame()


def _get_event_loop() -> asyncio.AbstractEventLoop:
    """Return the current event loop, or create a new one."""
    try:
        if is_ipython():
            nest_asyncio.apply()
        loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop


def _calc_split_ranges(
    start: datetime,
    end: datetime,
    split_delta: pd.Timedelta,
) -> list[tuple[datetime, datetime]]:
    """Return a list of time ranges split by `split_delta`."""
    # Use pandas date_range and split the result into 2 iterables
    s_ranges, e_ranges = tee(pd.date_range(start, end, freq=split_delta))
    next(e_ranges, None)  # skip to the next item in the 2nd iterable
    # Zip them together to get a list of (start, end) tuples of ranges
    # Note: we subtract 1 nanosecond from the 'end' value of each range so
    # to avoid getting duplicated records at the boundaries of the ranges.
    # Some providers don't have nanosecond granularity so we might
    # get duplicates in these cases
    ranges: list[tuple[datetime, datetime]] = [
        (s_time, e_time - pd.Timedelta("1ns"))
        for s_time, e_time in zip(s_ranges, e_ranges)
    ]

    # Since the generated time ranges are based on deltas from 'start'
    # we need to adjust the end time on the final range.
    # If the difference between the calculated last range end and
    # the query 'end' that the user requested is small (< 0.1% of a delta),
    # we just replace the last "end" time with our query end time.
    if (end - ranges[-1][1]) < (split_delta / 1000):
        ranges[-1] = ranges[-1][0], pd.Timestamp(end)
    else:
        # otherwise append a new range starting after the last range
        # in ranges and ending in 'end"
        # note - we need to add back our subtracted 1 nanosecond
        ranges.append((ranges[-1][1] + pd.Timedelta("1ns"), pd.Timestamp(end)))

    return ranges
