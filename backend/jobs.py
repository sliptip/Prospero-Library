import json
import logging
from threading import Event, RLock, Thread

from .database import now
from .patches import check_all
from .scanner import MetadataError

logger = logging.getLogger(__name__)


class Jobs:
    def __init__(self, database):
        self.database = database
        self.lock = RLock()
        self.stop = Event()
        self.thread = None
        self.active = None
        self.pending_checks = set()
        self.closed = False

    def check_new_game(self, game_id):
        with self.lock:
            self.pending_checks.add(game_id)
            self._start_pending_checks()

    def _start_pending_checks(self):
        if self.closed or self.active is not None or not self.pending_checks:
            return
        self.start("updates", check_all, game_ids=sorted(self.pending_checks))
        self.pending_checks.clear()

    def start(self, kind, worker, **options):
        with self.lock:
            if self.active is not None:
                raise RuntimeError("Дождитесь завершения текущей операции")
            self.stop.clear()
            report = {"found": 0, "added": 0, "updated": 0, "skipped": 0, "errors": 0, "issues": []}
            with self.database.connect() as db:
                job_id = db.execute(
                    "INSERT INTO jobs (kind,status,report,started_at) VALUES (?,?,?,?)",
                    (kind, "running", json.dumps(report), now()),
                ).lastrowid
            self.active = job_id
            self.thread = Thread(target=self._run, args=(job_id, kind, worker, report, options), daemon=True)
            self.thread.start()
            return self.get(job_id)

    def _run(self, job_id, kind, worker, report, options):
        def publish():
            with self.database.connect() as db:
                db.execute("UPDATE jobs SET report=? WHERE id=?", (json.dumps(report, ensure_ascii=False), job_id))

        state = "done"
        logger.info("Операция №%s (%s) начата", job_id, kind)
        try:
            worker(self.database, report, self.stop, publish, **options)
        except MetadataError as error:
            state = "error"
            report["issues"].append(str(error))
            logger.warning("Операция №%s: %s", job_id, error)
        except Exception:
            logger.exception("Ошибка операции №%s", job_id)
            state = "error"
            report["issues"].append("Ошибка операции. Подробности в локальном журнале.")
        finally:
            report.pop("current", None)
            with self.lock:
                if state == "done" and self.stop.is_set():
                    state = "cancelled"
                logger.info("Операция №%s (%s): %s; найдено %s, добавлено %s, обновлено %s, пропущено %s, ошибок %s",
                            job_id, kind, state, report["found"], report["added"], report["updated"],
                            report["skipped"], report["errors"])
                try:
                    with self.database.connect() as db:
                        db.execute("UPDATE jobs SET report=?,status=?,finished_at=? WHERE id=?",
                                   (json.dumps(report, ensure_ascii=False), state, now(), job_id))
                finally:
                    self.active = None
                self._start_pending_checks()

    def get(self, job_id=None):
        with self.lock, self.database.connect() as db:
            if job_id is None:
                row = db.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 1").fetchone()
            else:
                row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["report"] = json.loads(result["report"])
            return result

    def cancel(self, job_id):
        with self.lock:
            if self.active != job_id:
                return self.get(job_id)
            self.stop.set()
            with self.database.connect() as db:
                db.execute("UPDATE jobs SET status='cancelling' WHERE id=?", (job_id,))
            return self.get(job_id)

    def close(self):
        with self.lock:
            self.closed = True
            self.stop.set()
            thread = self.thread
        if thread:
            thread.join(timeout=50)
