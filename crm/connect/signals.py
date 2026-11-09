from django.dispatch import Signal

# Sent by the worker with connection, capability, records (a list of Record),
# and source ("sync" or "webhook"). Features subscribe to store or match records;
# the framework itself keeps nothing beyond the run or event that produced them.
# The worker calls receivers in one database transaction. Storage failures must
# propagate so the batch rolls back and its cursor or encrypted payload can be retried.
records_received = Signal()

# Sent by the worker's scheduler on each tick, with now. A receiver queues its own
# periodic work and returns how many tasks it queued.
worker_ticked = Signal()
