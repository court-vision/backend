"""
The data behind the agent's query_stats tool (docs/QUERY_STATS_PLAN.md).

    catalog.py  the metric catalog: every stat the tool can name, with the SQL
                that computes it at each grain over the nba.ask_* views
    reader.py   the connection the compiled SQL runs on: the read-only
                ask_reader role, or the main pool in a read-only transaction

The compiler and the tool itself (step 2 of the plan) build on these two.
"""
