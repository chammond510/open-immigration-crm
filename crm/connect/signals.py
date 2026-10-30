from django.dispatch import Signal

# Sent by the worker with connection, capability, records (a list of Record),
# and source ("sync" or "webhook"). Features subscribe to store or match records;
# the framework itself keeps nothing beyond the run or event that produced them.
records_received = Signal()
