"""The agentic layer, in bases: a record/replay model gateway, the tool registry and a plain tool-calling loop.

Nothing here can cause an external side effect except ``create_action``, which only proposes to
the approval gate. With ``LOANDESK_AGENT_MODE=off`` (the default) no model is ever called.
"""
