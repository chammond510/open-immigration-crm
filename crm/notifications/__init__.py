"""Alerts for staff, starting with work, matters, and contacts someone else gives them.

When a person saves a work item, matter, or contact with a new assignee who is
someone else, the assignee gets an in-app notification and, if they asked for
it in their profile and the firm can send email, a short email that names no
client. Changes the CRM makes on its own, such as calendar syncs, notify nobody.
Imports and hand-overs at deactivation run quietly; a hand-over sends one
summary instead.
"""
