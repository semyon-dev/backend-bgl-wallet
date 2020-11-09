from pybgl.functions.tools import bytes_to_int
from pybgl.functions.tools import c_int_to_int
from pybgl.functions.tools import c_int_len
from pybgl.functions.tools import rh2s
from pybgl.functions.tools import int_to_bytes
from pybgl.connector.utils import chunks_by_count
from pybgl.functions.script import parse_script
from collections import deque
from pybgl  import MRU, LRU
import time
import pickle
import asyncio


try: import rocksdb
except: pass

try: import plyvel
except: pass


class UTXO():

    def __init__(self, db_type, db,  rpc, loop, log, cache_size):
        self.cache = MRU()  # utxo cache
        self.restore_blocks_cache = LRU()  # blocks cache for restore utxo cache

        self.missed = set()  # missed utxo
        self.missed_failed = deque()
        self.loaded = dict()   # loaded from db missed records

        self.utxo_records = deque()  # prepared utxo records for write to db
        self.p2pkMapHash = deque()  # prepared utxo records for write to db
        self.pending_saved = dict()  # temp hash table, while records write process

        self.scheduled_to_delete = deque()
        self.pending_deleted = deque()


        self.checkpoint = 0
        self.checkpoints = deque()
        self.log = log


        self.size_limit = cache_size
        self.db_type = db_type
        self.db = db
        self.loop = loop

        self.save_process = False
        self.write_to_db = False

        self.rpc = rpc

        # stats
        self._requests = 0
        self._failed_requests = 0
        self._hit = 0
        self.saved_utxo_count = 0
        self.last_block = 0
        self.deleted_utxo_count = 0
        self.read_from_db_time = 0
        self.read_from_db_batch_time = 0
        self.read_from_db_count = 0
        self.read_from_db_time_total = 0
        self.loaded_utxo_count = 0


    def get(self, key):
        #
        # get and destroy unspent coin from cache
        # in case coin in pending saved list, schedule to delete this coin from db
        # in case coin not exist add to missed coin list
        #
        self._requests += 1
        i = None

        try:
            i = self.cache.delete(key)
        except:
            try:
                i = self.pending_saved[key]
                self.scheduled_to_delete.append(key)
            except:
                pass

        if i is None:
            self._failed_requests += 1
            self.missed.add(key)
        else:
            self._hit += 1
        return i


    def set(self, outpoint, pointer, amount, address):
        self.cache[outpoint] = (pointer, amount, address)


    async def load_utxo(self):
        #
        # load missed utxo from db
        #
        try:
            t = time.time()
            self.missed_failed = list()
            failed = False
            async with self.db.acquire() as conn:
                rows = await conn.fetch("SELECT outpoint, "
                                        "       pointer,"
                                        "       address,"
                                        "       amount "
                                        "FROM connector_utxo "
                                        "WHERE outpoint = ANY($1);", self.missed)
            for row in rows:
                self.loaded[row["outpoint"]] = (row["pointer"],
                                                row["amount"],
                                                row["address"])
                self.loaded_utxo_count += 1

            if len(self.missed) > len(rows):
                failed = True
                for row in rows:
                    self.missed.remove(row["outpoint"])

            self.read_from_db_count += len(self.missed)
            self.read_from_db_time += time.time() - t
            self.read_from_db_batch_time += time.time() - t
            self.read_from_db_time_total += time.time() - t
            if failed:
                self.missed_failed = list(self.missed)
            self.missed= set()
        except:
            raise


    async def load_utxo_from_daemon(self):
        #
        #  load missed utxo from bitcoind daemon
        #
        if not self.missed_failed: return
        missed = chunks_by_count(self.missed_failed, 50)
        for m in missed:
            result = await self.rpc.batch([["getrawtransaction", rh2s(i[:32]), 1] for i in m])
            hash_list = set()
            for r in result:
                if r["result"]["blockhash"] not in self.restore_blocks_cache:
                    hash_list.add(r["result"]["blockhash"])

            result2 = await self.rpc.batch([["getblock", r] for r in hash_list])
            for r in result2:
               self.restore_blocks_cache[r["result"]["hash"]] = r["result"]

            for key, r in zip(m, result):
                out_index = bytes_to_int(key[32:])
                tx=r["result"]
                amount = int(tx["vout"][out_index]["value"] * 100000000)
                script = parse_script(tx["vout"][out_index]["scriptPubKey"]["hex"])
                try:
                    address = b"".join((bytes([script["nType"]]), script["addressHash"]))
                except:
                    address = b"".join((bytes([script["nType"]]), script["script"]))
                block = self.restore_blocks_cache[tx["blockhash"]]

                tx_index = block["tx"].index(tx["txid"])
                block_height = block["height"]
                pointer = (block_height << 39) + (tx_index << 20) + (1 << 19) + out_index
                self.loaded[key] = (pointer, amount, address)
        self.missed_failed = list()
        while len(self.restore_blocks_cache) > 1000:
            self.restore_blocks_cache.pop()


    def get_loaded(self, key):
        try:
            i = self.loaded.pop(key)
            self.scheduled_to_delete.append(key)
            return i
        except:
            return None


    def create_checkpoint(self, last_block, app_last_block = None):
        # check checkpoints state
        self.last_block = last_block
        if  not self.checkpoints: return
        checkpoints = set()
        for i in self.checkpoints:
            if i > self.checkpoint: checkpoints.add(i)
        self.checkpoints = sorted(checkpoints)
        # save to db tail from cache
        if  self.save_process or not self.cache: return
        if app_last_block is not None:
            if app_last_block < self.checkpoints[0]: return

        self.save_process = True
        limit = 0
        try:
            checkpoint = self.checkpoints.pop(0)
            lb = 0
            while self.cache:
                key, value = self.cache.peek_last_item()
                if value[0] >> 39 != lb:
                    # block changed

                    if checkpoint <= lb:
                        # last block was checkpoint block
                        if len(self.utxo_records) > self.size_limit * 0.9:
                            limit = self.size_limit
                        else:
                            limit = self.size_limit * 0.9
                        if len(self.cache) < limit:
                            break

                        if self.checkpoints:
                            if app_last_block is None:
                                # no app checkpoint constraint
                                checkpoint = self.checkpoints.pop(0)
                            elif app_last_block > self.checkpoints[0]:
                                # app checkpoint ahead of utxo checkpoint
                                checkpoint = self.checkpoints.pop(0)
                            else:
                                break
                        else:
                            # no more checkpoints
                            break

                lb = value[0] >> 39

                self.cache.delete(key)
                self.utxo_records.append((key, value[0], value[2], value[1]))
                self.pending_saved[key] = value
            self.last_checkpoint = self.checkpoint
            self.checkpoint = lb

            self.pending_deleted = deque(self.scheduled_to_delete)
            self.scheduled_to_delete = deque()

            self.log.debug("checkpoint %s cache size %s limit %s" % (self.checkpoint,
                                                                     len(self.cache),
                                                                     limit))
        except:
            self.log.critical("create checkpoint error")


    async def commit(self):
        # save to db tail from cache
        if  not self.checkpoint: return
        if  self.write_to_db: return

        try:
            self.write_to_db = True
            t = time.time()
            if not self.checkpoint: return
            await self.postgresql_atomic_batch()
            self.log.debug("utxo checkpoint saved time %s" % round(time.time()-t, 4))
            self.saved_utxo_count += len(self.utxo_records)
            self.deleted_utxo_count += len(self.pending_deleted)
            self.pending_deleted = deque()
            self.utxo_records = deque()
            self.pending_saved = dict()

        except Exception as err:
            self.log.critical("save_checkpoint error: %s" % str(err))
        finally:
            self.save_process = False
            self.write_to_db = False


    async def postgresql_atomic_batch(self):
        async with self.db.acquire() as conn:
            async with conn.transaction():
               if self.pending_deleted:
                   await conn.execute("DELETE FROM connector_utxo WHERE "
                                      "outpoint = ANY($1);", self.pending_deleted)
               if self.utxo_records:
                   await conn.copy_records_to_table('connector_utxo',
                                                    columns=["outpoint",
                                                             "pointer",
                                                             "address",
                                                             "amount"],
                                                    records=self.utxo_records)
               if self.p2pkMapHash:
                   await conn.executemany("INSERT INTO connector_p2pk_map (address, script) "
                                          "VALUES ($1, $2) ON CONFLICT DO NOTHING;", self.p2pkMapHash)
                   self.p2pkMapHash = deque()
               await conn.execute("UPDATE connector_utxo_state SET value = $1 "
                                  "WHERE name = 'last_block';", self.checkpoint)
               await conn.execute("UPDATE connector_utxo_state SET value = $1 "
                                  "WHERE name = 'last_cached_block';", self.last_block)


    def len(self):
        return len(self.cache)


    def hit_rate(self):
        if self._requests:
            return self._hit / self._requests
        else:
            return 0


class UUTXO():
    def __init__(self, db_type, db, block_filters, log):
        self.load_buffer = deque()
        self.loaded_utxo = LRU(100000)  # loaded from db missed records
        self.loaded_ustxo = LRU(100000)  # loaded from db missed records

        self.load_data_future = asyncio.Future()
        self.load_data_future.set_result(True)

        self.log = log
        self.db_type = db_type
        self.db = db
        self.block_filters = block_filters





    async def load_utxo_data(self):
        #
        # load missed utxo from db
        #
        # print("load_utxo_data")
        while True:
            if not self.load_data_future.done():
                await self.load_data_future
                continue
            break
        try:
            self.load_data_future = asyncio.Future()
            load_utxo = set(self.load_buffer)
            load_stxo = set(self.load_buffer)
            self.load_buffer = deque()

            async with self.db.acquire() as conn:
                rows = await conn.fetch("SELECT outpoint, "
                                        "       pointer,"
                                        "       address,"
                                        "       amount "
                                        "FROM connector_utxo "
                                        "WHERE outpoint = ANY($1);", load_utxo)

            for row in rows:
                self.loaded_utxo[row["outpoint"]] = (row["pointer"],
                                                     row["amount"],
                                                     row["address"])
                load_utxo.remove(row["outpoint"])

            if load_utxo:
                async with self.db.acquire() as conn:
                    rows = await conn.fetch("SELECT outpoint, "
                                            "       address,"
                                            "       amount "
                                            "FROM connector_unconfirmed_utxo "
                                            "WHERE outpoint = ANY($1);", load_utxo)

                for row in rows:
                    self.loaded_utxo[row["outpoint"]] = (None,
                                                         row["amount"],
                                                         row["address"])


            async with self.db.acquire() as conn:
                rows = await conn.fetch("SELECT outpoint, "
                                        "       sequence,"
                                        "       tx_id "
                                        "FROM connector_unconfirmed_stxo "
                                        "WHERE outpoint = ANY($1);", load_stxo)

            for row in rows:
                try:
                    self.loaded_ustxo[row["outpoint"]].append((row["tx_id"],
                                                              row["sequence"]))
                except:
                    self.loaded_ustxo[row["outpoint"]] = [(row["tx_id"],
                                                          row["sequence"])]

        except Exception as err:
            self.log.error("load_utxo_data failed %s" % err)
        finally:
            self.load_data_future.set_result(True)


    async def commit_tx(self, commit_uutxo, commit_ustxo, commit_up2pk_map, conn):
        if commit_uutxo:
            await conn.copy_records_to_table('connector_unconfirmed_utxo',
                                             columns=["outpoint",
                                                      "out_tx_id",
                                                      "address",
                                                      "amount"],
                                             records=commit_uutxo)
        if commit_up2pk_map:
            await conn.copy_records_to_table('connector_unconfirmed_p2pk_map',
                                             columns=["tx_id",
                                                      "address",
                                                      "script"],
                                             records=commit_up2pk_map)

        while commit_ustxo:
            rows = await conn.fetch("INSERT  INTO connector_unconfirmed_stxo "
                                    "(outpoint, sequence, out_tx_id, tx_id, input_index, address) "
                                    " (SELECT r.outpoint,"
                                    "         r.sequence,"
                                    "         r.out_tx_id,"
                                    "         r.tx_id,"
                                    "         r.input_index, "
                                    "         r.address "
                                    "FROM unnest($1::connector_unconfirmed_stxo[]) as r ) "
                                    "ON CONFLICT (outpoint, sequence) DO NOTHING "
                                    "            RETURNING outpoint as o,"
                                    "                      sequence as s,"
                                    "                      out_tx_id as ot,"
                                    "                      tx_id as t,"
                                    "                      input_index as i,"
                                    "                      address as a;" , commit_ustxo)

            for row in rows:
                commit_ustxo.remove((row["o"], row["s"], row["ot"], row["t"], row["i"], row["a"]))

            # in case double spend increment sequence
            commit_ustxo = set((i[0], i[1] + 1, i[2], i[3], i[4], i[5]) for i in commit_ustxo)

    async def apply_block_changes(self, txs, h, conn):

        tx_filters = dict()
        # handle block new coins
        rows = await conn.fetch("DELETE FROM connector_unconfirmed_p2pk_map  "
                                "WHERE tx_id = ANY($1) "
                                "RETURNING  address, script, tx_id;", txs)

        p2pk_map = deque()
        p2pk_map_backup = deque()
        for row in rows:
            p2pk_map.append((row["address"], row["script"]))
            p2pk_map_backup.append((row["tx_id"], row["address"], row["script"]))

        if p2pk_map:
            await conn.executemany("INSERT INTO connector_p2pk_map (address, script) "
                                   "VALUES ($1, $2) ON CONFLICT DO NOTHING;", p2pk_map)


        # remove all block created coins from connector_unconfirmed_utxo table
        # add new coins to connector_utxo table

        rows = await conn.fetch("DELETE FROM connector_unconfirmed_utxo  "
                                "WHERE out_tx_id = ANY($1) "
                                "RETURNING  outpoint, "
                                "           out_tx_id as t,"
                                "           address, "
                                "           amount;", txs)

        batch, uutxo = deque(), deque()
        for r in rows:
            batch.append((r["outpoint"],
                         (h << 39) + (txs.index(r["t"]) << 20) + (1 << 19) + bytes_to_int(r[32:]),
                         r["address"], r["amount"]))
            uutxo.append((r["outpoint"], r["t"], r["address"], r["amount"]))
            if self.block_filters:
                try:
                    tx_filters[txs.index(r["t"])].append(r["address"])
                except:
                    tx_filters[txs.index(r["t"])] = [r["address"]]

        await conn.copy_records_to_table('connector_utxo',
                                         columns=["outpoint", "pointer",
                                                  "address", "amount"], records=batch)

        # handle block destroyed coins
        # remove all destroy records from  connector_unconfirmed_stxo

        rows = await conn.fetch("DELETE FROM connector_unconfirmed_stxo "
                                "WHERE tx_id = ANY($1) "
                                "RETURNING outpoint,"
                                "          sequence,"
                                "          out_tx_id,"
                                "          tx_id,"
                                "          input_index as i,"
                                "          address as a;", txs)
        stxo, utxo, outpoints = deque(), deque(), set()
        for r in rows:
            stxo.append((r["outpoint"], r["sequence"], r["out_tx_id"], r["tx_id"], r["i"], r["a"]))
            outpoints.add(r["outpoint"])
            if self.block_filters:
                try:
                    tx_filters[txs.index(r["tx_id"])].append(r["a"])
                except:
                    tx_filters[txs.index(r["tx_id"])] = [r["a"]]
        if outpoints:
            rows = await conn.fetch("DELETE FROM connector_utxo WHERE outpoint = ANY($1) "
                                    "RETURNING outpoint, pointer, address, amount;", outpoints)
            for r in rows:
                # save deleted utxo except utxo created in recent block
                if r["pointer"] >> 39 < h:
                    utxo.append((r["outpoint"], r["pointer"], r["address"], r["amount"]))

        #    delete dbs records
        dbs_stxo, dbs_uutxo, invalid_txs = deque(), deque(), set()

        if outpoints:
            rows = await conn.fetch("DELETE FROM connector_unconfirmed_stxo "
                                    "WHERE outpoint = ANY($1) "
                                    "RETURNING outpoint,"
                                    "          sequence,"
                                    "          out_tx_id,"
                                    "          tx_id,"
                                    "          input_index as i, "
                                    "          address as a;", outpoints)
            for r in rows:
                dbs_stxo.append((r["outpoint"], r["sequence"], r["out_tx_id"], r["tx_id"], r["i"], r["a"]))
                invalid_txs.add(r["tx_id"])

        # handle invalid transactions while invalid transactions list not empty
        #    remove coins for transactions list from connector_unconfirmed_utxo
        #    remove destroy records for transactions list from connector_unconfirmed_stxo
        #        get new invalid transactions list from deleted records

        block_invalid_txs = set()

        while invalid_txs:
            block_invalid_txs = block_invalid_txs | invalid_txs

            rows = await conn.fetch("DELETE FROM connector_unconfirmed_utxo  "
                                    "WHERE out_tx_id = ANY($1) "
                                    "RETURNING  outpoint, "
                                    "           out_tx_id as t,"
                                    "           address, "
                                    "           amount;", invalid_txs)
            outpoints = set()
            for r in rows:
                dbs_uutxo.append((r["outpoint"], r["t"], r["address"], r["amount"]))
                outpoints.add(r["outpoint"])

            rows = await conn.fetch("DELETE FROM connector_unconfirmed_stxo "
                                    "WHERE outpoint = ANY($1) "
                                    "RETURNING outpoint,"
                                    "          sequence,"
                                    "          out_tx_id,"
                                    "          tx_id,"
                                    "          input_index as i,"
                                    "          address as a;", outpoints)
            invalid_txs = set()
            for r in rows:
                dbs_stxo.append((r["outpoint"], r["sequence"], r["out_tx_id"], r["tx_id"], r["i"], r["a"]))
                invalid_txs.add(r["tx_id"])

        await conn.execute("INSERT INTO connector_block_state_checkpoint (height, data) "
                           "VALUES ($1, $2);", h, pickle.dumps({"utxo": utxo,
                                                                "uutxo": uutxo,
                                                                "stxo": stxo,
                                                                "p2pk_map": p2pk_map_backup,
                                                                "coinbase_tx_id": txs[0]}))

        return {"invalid_uutxo": dbs_uutxo,
                "invalid_stxo": dbs_stxo,
                "invalid_txs": block_invalid_txs,
                "stxo": stxo,
                "tx_filters": tx_filters,
                "coinbase_tx_id": txs[0]}


    async def rollback_block(self, conn):
        row = await conn.fetchrow("DELETE FROM connector_block_state_checkpoint "
                                  "WHERE height in "
                                  "(SELECT height FROM connector_block_state_checkpoint"
                                  " ORDER BY height DESC LIMIT 1) RETURNING "
                                  "height, data;")

        data = pickle.loads(row["data"])

        if data["p2pk_map"]:
            await conn.copy_records_to_table('connector_unconfirmed_p2pk_map',
                                             columns=["tx_id", "address", "script"],
                                             records=data["p2pk_map"])
            # skip delete from connector_p2pk_map, we can't determine if connector_p2pk_map
            # records was before recent block

        r = deque()
        for t in data["uutxo"]:
            if t[1] != data["coinbase_tx_id"]:
                r.append(t)
        await conn.copy_records_to_table('connector_unconfirmed_utxo',
                                         columns=["outpoint", "out_tx_id",
                                                  "address", "amount"], records=r)

        await conn.execute("DELETE FROM connector_utxo WHERE outpoint = ANY($1);",
                           deque(r[0] for r in data["uutxo"]))

        await conn.copy_records_to_table('connector_unconfirmed_stxo',
                                         columns=["outpoint", "sequence",
                                                  "out_tx_id", "tx_id", "input_index", "address"],
                                         records=data["stxo"])

        await conn.copy_records_to_table('connector_utxo',
                                         columns=["outpoint", "pointer",
                                                  "address", "amount"], records=data["utxo"])


        return {"height": int(row["height"]), "coinbase_tx_id": bytes(data["coinbase_tx_id"])}


    async def flush_mempool(self):
        self.log.info("Flushing mempool ...")
        async with self.db.acquire() as conn:
            async with conn.transaction():
                await conn.execute("truncate table  connector_unconfirmed_stxo;")
                await conn.execute("truncate table  connector_unconfirmed_utxo;")