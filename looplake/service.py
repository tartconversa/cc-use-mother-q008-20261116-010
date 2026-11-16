from __future__ import annotations

from .batch import BatchImport
from .dimensions import Dimensions
from .landing import Landing
from .ledger import Ledger
from .lineage import Lineage
from .mart import Mart
from .quality import Quality
from .reconcile import Reconciliation
from .release import Release
from .store import Store
from .settlement import Settlement
from .inventory import Inventory
from .query import Query
from .snapshot import SnapshotAudit


class LakeService:
    def __init__(self, database_path):
        self.store = Store(database_path)
        self.dimensions = Dimensions(self.store)
        self.landing = Landing(self.store, self.dimensions)
        self.ledger = Ledger(self.store, self.dimensions, self.landing)
        self.inventory = Inventory(self.ledger)
        self.query = Query(self.ledger)
        self.settlement = Settlement(self.store)
        self.reconciliation = Reconciliation(self.ledger, self.settlement)
        self.mart = Mart(self.ledger, self.reconciliation)
        self.releases = Release(self.store, self.mart, self.reconciliation, self.inventory)
        self.snapshots = SnapshotAudit(self.store)
        self.quality = Quality(self.store, self.landing, self.ledger,
                               self.reconciliation, self.mart)
        self.lineage = Lineage(self.store, self.ledger, self.reconciliation,
                               self.mart, self.releases)
        self.batches = BatchImport(self.ledger)

    def register_source(self, source, channel, expected_version=None):
        return self.dimensions.register_source(source, channel, expected_version)

    def register_dimension(self, data, expected_version=None):
        return self.dimensions.put(data, expected_version)

    def ingest(self, data):
        return self.ledger.ingest(data)

    def retry(self, ingestion_id):
        return self.ledger.normalize(ingestion_id)

    def batch_status(self, batch_id):
        return self.reconciliation.batch(batch_id)

    def month_status(self, month):
        return self.quality.month(month)

    def month_report(self, month):
        return self.mart.month(month)

    def preview(self, month):
        return self.releases.preview(month)

    def publish(self, name, month, fingerprint):
        return self.releases.publish(name, month, fingerprint)
