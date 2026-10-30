"""Чтение Parquet-датасетов с обработкой блокировок (активные файлы)."""

from __future__ import annotations

from pathlib import Path

import pyarrow.dataset as ds
import pyarrow.parquet as pq


class ParquetReader:
    """Безопасное чтение Parquet-данных."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    def load_dataset(self, dataset_name: str, exclude_active: bool = True) -> ds.FileSystemDataset:
        """
        Загружает датасет. Использует `dataset`, так как он умеет игнорировать
        поврежденные/открытые файлы, которые ParquetDataset не может прочитать из-за отсутствия footer.
        
        Args:
            dataset_name: Имя датасета (raw_events, feature_snapshots)
            exclude_active: Если True, исключает файлы без корректного Parquet footer
        """
        path = self.directory / dataset_name
        if not path.exists():
            raise FileNotFoundError(f"dataset {dataset_name} not found in {self.directory}")

        # Получаем список файлов
        files = sorted(list(path.glob("*.parquet")))
        
        if exclude_active:
            # New writers publish *.parquet atomically. For legacy files, verify
            # the footer rather than silently discarding the newest completed file.
            completed = []
            for file in files:
                try:
                    pq.read_metadata(file)
                except (OSError, ValueError):
                    continue
                completed.append(file)
            files = completed

        if not files:
            raise FileNotFoundError(f"no completed parquet files in {path}")

        # Используем ds.dataset с явным списком файлов
        return ds.dataset(files, format="parquet", partitioning="hive")

    def list_files(self, dataset_name: str) -> list[Path]:
        return sorted(list((self.directory / dataset_name).glob("*.parquet")))
