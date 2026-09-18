# Use synchronously durable AWS Valkey

AWS-managed Valkey Resources use node-based ElastiCache Valkey 9 or later with synchronous
Multi-AZ durability, cluster mode, one shard, and one cross-AZ replica. Gimme does not offer Redis
OSS or weaker ElastiCache durability modes: ordinary replication can lose acknowledged writes,
while synchronous durability persists them before acknowledgement. Because cluster mode changes
the application contract, bindings use an explicit versioned Laravel cluster adapter with
capability-specific validation; older applications remain eligible for supported cache, session,
and standard queue uses, while Horizon requires versions with first-class cluster support.
