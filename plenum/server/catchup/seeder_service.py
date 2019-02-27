from typing import Any, Tuple, Optional

from plenum.common.channel import RxChannel
from plenum.common.ledger import Ledger
from plenum.common.messages.node_messages import CatchupReq, CatchupRep, ConsistencyProof, LedgerStatus
from plenum.common.util import SortedDict
from plenum.server.catchup.utils import CatchupDataProvider, build_ledger_status
from stp_core.common.log import getlogger

logger = getlogger()


class SeederService:
    def __init__(self, input: RxChannel, provider: CatchupDataProvider,
                 echo_ledger_status_if_up_to_date: bool):
        input.set_handler(LedgerStatus, self.process_ledger_status)
        input.set_handler(CatchupReq, self.process_catchup_req)

        self._provider = provider
        self._ledgers = {}  # Dict[int, Ledger]
        self._echo_ledger_status_if_up_to_date = echo_ledger_status_if_up_to_date

    def __repr__(self):
        return self._provider.node_name()

    def add_ledger(self, ledger_id: int, ledger: Ledger):
        self._ledgers[ledger_id] = ledger

    def process_ledger_status(self, status: LedgerStatus, frm: str):
        logger.info("{} received ledger status: {} from {}".format(self, status, frm))

        ledger_id, ledger = self._get_ledger_and_id(status)

        if ledger is None:
            logger.warning("{} discarding message {} from {} because it references invalid ledger".
                           format(self, status, frm))
            return

        if status.txnSeqNo < 0:
            logger.warning("{} discarding message {} from {} because it contains negative sequence number".
                           format(self, status, frm))
            return

        if status.txnSeqNo >= ledger.size:
            if self._echo_ledger_status_if_up_to_date:
                ledger_status = build_ledger_status(ledger_id, ledger, self._provider)
                self._provider.send_to(ledger_status, frm)
            return

        try:
            cons_proof = self._build_consistency_proof(ledger_id, status.txnSeqNo, ledger.size)

            logger.info("{} sending consistency proof: {} to {}".format(self, cons_proof, frm))
            self._provider.send_to(cons_proof, frm)
        except ValueError as e:
            logger.warning("{} discarding message {} from {} because {}".
                           format(self, status, frm, e))
            return

    def process_catchup_req(self, req: CatchupReq, frm: str):
        logger.info("{} received catchup request: {} from {}".format(self, req, frm))

        ledger_id, ledger = self._get_ledger_and_id(req)

        if ledger is None:
            logger.warning("{} discarding message {} from {} because it references invalid ledger".
                           format(self, req, frm))
            return

        start = req.seqNoStart
        end = req.seqNoEnd

        if start > end:
            logger.debug("{} discarding message {} from {} because its start greater than end".
                         format(self, req, frm))
            return

        if end > req.catchupTill:
            logger.debug("{} discarding message {} from {} because its end greater than catchup till".
                         format(self, req, frm))
            return

        if req.catchupTill > ledger.size:
            logger.debug("{} discarding message {} from {} because its catchup till greater than ledger size {}".
                         format(self, req, frm, ledger.size))
            return

        cons_proof = ledger.tree.consistency_proof(end, req.catchupTill)
        cons_proof = [Ledger.hashToStr(p) for p in cons_proof]

        txns = {}
        for seq_no, txn in ledger.getAllTxn(start, end):
            txns[seq_no] = self._provider.update_txn_with_extra_data(txn)

        txns = SortedDict(txns)  # TODO: Do we really need them sorted on the sending side?
        rep = CatchupRep(ledger_id, txns, cons_proof)
        message_splitter = self._make_splitter_for_catchup_rep(ledger, req.catchupTill)
        self._provider.send_to(rep, frm, message_splitter)

    def _get_ledger_and_id(self, req: Any) -> Tuple[int, Optional[Ledger]]:
        ledger_id = req.ledgerId
        return ledger_id, self._ledgers.get(ledger_id)

    @staticmethod
    def _make_consistency_proof(ledger: Ledger, seq_no_start: int, seq_no_end: int):
        proof = ledger.tree.consistency_proof(seq_no_start, seq_no_end)
        string_proof = [Ledger.hashToStr(p) for p in proof]
        return string_proof

    def _build_consistency_proof(self, ledger_id: int, seq_no_start: int, seq_no_end: int) -> ConsistencyProof:
        ledger = self._ledgers[ledger_id]

        if seq_no_end < seq_no_start:
            raise ValueError("end {} is less than start {}".format(seq_no_end, seq_no_start))

        if seq_no_start > ledger.size:
            raise ValueError("start {} is more than ledger size {}".format(seq_no_start, ledger.size))

        if seq_no_end > ledger.size:
            raise ValueError("end {} is more than ledger size {}".format(seq_no_end, ledger.size))

        if seq_no_start == 0:
            # Consistency proof for an empty tree cannot exist. Using the root
            # hash now so that the node which is behind can verify that
            # TODO: Make this an empty list
            old_root = ledger.tree.root_hash
            old_root = Ledger.hashToStr(old_root)
            proof = [old_root, ]
        else:
            proof = self._make_consistency_proof(ledger, seq_no_start, seq_no_end)
            old_root = ledger.tree.merkle_tree_hash(0, seq_no_start)
            old_root = Ledger.hashToStr(old_root)

        new_root = ledger.tree.merkle_tree_hash(0, seq_no_end)
        new_root = Ledger.hashToStr(new_root)

        # TODO: Delete when INDY-1946 gets implemented
        three_pc_key = self._provider.three_phase_key_for_txn_seq_no(ledger_id, seq_no_end)
        view_no, pp_seq_no = three_pc_key if three_pc_key else (0, 0)

        return ConsistencyProof(ledger_id,
                                seq_no_start,
                                seq_no_end,
                                view_no,
                                pp_seq_no,
                                old_root,
                                new_root,
                                proof)

    def _make_splitter_for_catchup_rep(self, ledger, initial_seq_no):

        def _split(message):
            txns = list(message.txns.items())
            if len(message.txns) < 2:
                logger.warning("CatchupRep has {} txn(s). This is not enough "
                               "to split. Message: {}".format(len(message.txns), message))
                return None
            divider = len(message.txns) // 2
            left = txns[:divider]
            left_last_seq_no = left[-1][0]
            right = txns[divider:]
            right_last_seq_no = right[-1][0]
            left_cons_proof = self._make_consistency_proof(ledger,
                                                           left_last_seq_no,
                                                           initial_seq_no)
            right_cons_proof = self._make_consistency_proof(ledger,
                                                            right_last_seq_no,
                                                            initial_seq_no)
            ledger_id = message.ledgerId

            left_rep = CatchupRep(ledger_id, SortedDict(left), left_cons_proof)
            right_rep = CatchupRep(ledger_id, SortedDict(right), right_cons_proof)
            return left_rep, right_rep

        return _split


class ClientSeederService(SeederService):
    def __init__(self, input: RxChannel, provider: CatchupDataProvider):
        SeederService.__init__(self, input, provider, echo_ledger_status_if_up_to_date=True)


class NodeSeederService(SeederService):
    def __init__(self, input: RxChannel, provider: CatchupDataProvider):
        SeederService.__init__(self, input, provider, echo_ledger_status_if_up_to_date=False)
