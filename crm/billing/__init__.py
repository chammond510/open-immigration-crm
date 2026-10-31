"""Invoices, rates, and manually recorded payments.

Visible to staff whose role includes Billing (the "Can view and manage billing"
permission); voids and rates need "Can void billing and set rates". Payment
connectors later add payment links; recording which account received a payment
is the extent of trust accounting in core.
"""
