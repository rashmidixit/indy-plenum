import pytest

from plenum.common.constants import DOMAIN_LEDGER_ID
from plenum.server.replica import Replica
from plenum.test import waits
from plenum.test.delayers import cDelay
from plenum.test.helper import sdk_send_random_requests, assertExp
from plenum.test.test_node import getNonPrimaryReplicas, getPrimaryReplica, \
    ensureElectionsDone
from plenum.test.view_change.helper import ensure_view_change
from stp_core.loop.eventually import eventually

nodeCount = 4

CHK_FREQ = 5
LOG_SIZE = 3 * CHK_FREQ


@pytest.fixture(scope='function')
def view_change_done(looper, txnPoolNodeSet):
    ensure_view_change(looper, txnPoolNodeSet)
    ensureElectionsDone(looper=looper, nodes=txnPoolNodeSet)


@pytest.fixture(scope='function',
                params=['primary', 'non-primary'])
def one_replica_and_others_in_backup_instance(
        request, txnPoolNodeSet, view_change_done):

    backup_inst_id = 1

    primary = getPrimaryReplica(txnPoolNodeSet, backup_inst_id)
    non_primaries = getNonPrimaryReplicas(txnPoolNodeSet, backup_inst_id)

    if request.param == 'primary':
        return primary, non_primaries
    else:
        return non_primaries[0], [primary] + non_primaries[1:]


def test_backup_replica_resumes_ordering_on_lag_in_checkpoints(
        looper, chkFreqPatched, reqs_for_checkpoint,
        sdk_pool_handle, sdk_wallet_client,
        one_replica_and_others_in_backup_instance):
    """
    Verifies resumption of ordering 3PC-batches on a backup replica
    on detection of a lag in checkpoints
    """

    slow_replica, other_replicas = one_replica_and_others_in_backup_instance
    view_no = slow_replica.viewNo

    # Send a request
    sdk_send_random_requests(looper, sdk_pool_handle, sdk_wallet_client, 1)

    # Ensure that the backup replica has ordered the batch for it
    looper.run(
        eventually(lambda: assertExp(slow_replica.last_ordered_3pc == (view_no, 1)),
                   retryWait=1,
                   timeout=waits.expectedTransactionExecutionTime(4)))

    # Don't receive Commits from two replicas
    slow_replica.node.nodeIbStasher.delay(
        cDelay(instId=1, sender_filter=other_replicas[0].node.name))
    slow_replica.node.nodeIbStasher.delay(
        cDelay(instId=1, sender_filter=other_replicas[1].node.name))

    # Send a request for which the replica will not be able to order the batch
    # due to an insufficient count of Commits
    sdk_send_random_requests(looper, sdk_pool_handle, sdk_wallet_client, 1)

    looper.runFor(waits.expectedTransactionExecutionTime(4))

    # Recover reception of Commits
    slow_replica.node.nodeIbStasher.resetDelays()

    # Send requests but in a quantity insufficient
    # for catch-up number of checkpoints
    sdk_send_random_requests(looper, sdk_pool_handle, sdk_wallet_client,
                             Replica.STASHED_CHECKPOINTS_BEFORE_CATCHUP *
                             reqs_for_checkpoint - 2)

    # Ensure that the replica has not ordered the batches
    looper.runFor(waits.expectedTransactionExecutionTime(4))
    assert slow_replica.last_ordered_3pc == (view_no, 1)

    # Ensure that the watermarks have not been shifted since the view start
    assert slow_replica.h == 0
    assert slow_replica.H == 15

    # Ensure that the collections related to requests, batches and
    # own checkpoints are not empty.
    # (Note that a primary replica removes requests from requestQueues
    # when creating a batch with them.)
    if slow_replica.isPrimary:
        assert slow_replica.sentPrePrepares
    else:
        assert slow_replica.requestQueues[DOMAIN_LEDGER_ID]
        assert slow_replica.prePrepares
    assert slow_replica.prepares
    assert slow_replica.commits
    assert slow_replica.batches
    assert slow_replica.checkpoints

    # Ensure that there are some quorumed stashed checkpoints
    assert slow_replica.stashed_checkpoints_with_quorum()

    # Send more requests to reach catch-up number of checkpoints
    sdk_send_random_requests(looper, sdk_pool_handle, sdk_wallet_client,
                             reqs_for_checkpoint)

    # Ensure that the replica has adjusted last_ordered_3pc to the end
    # of the last checkpoint
    looper.run(
        eventually(lambda: assertExp(slow_replica.last_ordered_3pc ==
                                     (view_no, (Replica.STASHED_CHECKPOINTS_BEFORE_CATCHUP + 1) * CHK_FREQ)),
                   retryWait=1,
                   timeout=waits.expectedTransactionExecutionTime(4)))

    # Ensure that the watermarks have been shifted so that the lower watermark
    # has the same value as last_ordered_3pc
    assert slow_replica.h == (Replica.STASHED_CHECKPOINTS_BEFORE_CATCHUP + 1) * CHK_FREQ
    assert slow_replica.H == (Replica.STASHED_CHECKPOINTS_BEFORE_CATCHUP + 1) * CHK_FREQ + 15

    # Ensure that the collections related to requests, batches and
    # own checkpoints have been cleared
    assert not slow_replica.requestQueues[DOMAIN_LEDGER_ID]
    assert not slow_replica.sentPrePrepares
    assert not slow_replica.prePrepares
    assert not slow_replica.prepares
    assert not slow_replica.commits
    assert not slow_replica.batches
    assert not slow_replica.checkpoints

    # Ensure that now there are no quorumed stashed checkpoints
    assert not slow_replica.stashed_checkpoints_with_quorum()

    # Send a request
    sdk_send_random_requests(looper, sdk_pool_handle, sdk_wallet_client, 1)

    # Ensure that the replica has ordered the batch for the sent request
    looper.run(
        eventually(lambda: assertExp(slow_replica.last_ordered_3pc ==
                                     (view_no, (Replica.STASHED_CHECKPOINTS_BEFORE_CATCHUP + 1) * CHK_FREQ + 1)),
                   retryWait=1,
                   timeout=waits.expectedTransactionExecutionTime(4)))
