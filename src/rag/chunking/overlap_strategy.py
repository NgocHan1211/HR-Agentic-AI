"""
Overlap strategies for chunking.
Implements overlap mechanisms to preserve context between chunks (MVP: character-based).
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from .chunk_metadata import Chunk, HeadingContext
from ..parsers.base_parser import SourceRef, SourceLocation, BlockType, ContentBlock


@dataclass
class OverlapConfig:
    """Configuration for overlap strategy."""
    
    overlap_type: str = "character"  # "character" for MVP
    overlap_chars: int = 100  # Number of overlapping characters
    min_chunk_size: int = 50  # Minimum chunk size to apply overlap
    
    def __post_init__(self):
        if self.overlap_chars < 0:
            raise ValueError("overlap_chars must be >= 0")
        if self.min_chunk_size < 1:
            raise ValueError("min_chunk_size must be >= 1")


class OverlapStrategy(ABC):
    """Abstract base class for overlap strategies."""
    
    @abstractmethod
    def apply_overlap(
        self,
        current_chunk: Chunk,
        previous_chunk: Optional[Chunk]
    ) -> Chunk:
        """
        Apply overlap to current chunk based on previous chunk.
        
        Args:
            current_chunk: The current chunk to add overlap to
            previous_chunk: The previous chunk (if any)
        
        Returns:
            Modified chunk with overlap applied
        """
        pass


class CharacterOverlapStrategy(OverlapStrategy):
    """
    MVP overlap strategy: character-based overlap.
    
    Takes the last N characters from the previous chunk and adds them
    to the beginning of the current chunk for context preservation.
    """
    
    def __init__(self, config: OverlapConfig):
        """Initialize with configuration."""
        self.config = config
    
    def apply_overlap(
        self,
        current_chunk: Chunk,
        previous_chunk: Optional[Chunk]
    ) -> Chunk:
        """
        Apply character-based overlap.
        
        If previous chunk exists and current chunk is large enough,
        extract last N characters from previous chunk and prepend to current.
        
        Args:
            current_chunk: The current chunk to add overlap to
            previous_chunk: The previous chunk (if any)
        
        Returns:
            Modified chunk with overlap applied
        """
        if previous_chunk is None:
            return current_chunk
        
        if current_chunk.char_count < self.config.min_chunk_size:
            return current_chunk
        
        if previous_chunk.char_count < self.config.overlap_chars:
            overlap_text = previous_chunk.text
            overlap_count = len(overlap_text)
        else:
            overlap_text = previous_chunk.text[-self.config.overlap_chars:]
            overlap_count = self.config.overlap_chars
        
        return Chunk(
            chunk_id=current_chunk.chunk_id,
            source_ref=current_chunk.source_ref,
            text=current_chunk.text,
            block_ids=current_chunk.block_ids,
            location=current_chunk.location,
            heading_context=current_chunk.heading_context,
            block_types=current_chunk.block_types,
            char_count=current_chunk.char_count,
            metadata=current_chunk.metadata,
            created_at=current_chunk.created_at,
            overlap_text=overlap_text,
            overlap_char_count=overlap_count
        )


class NoOverlapStrategy(OverlapStrategy):
    """No-op overlap strategy for testing or when overlap is not needed."""
    
    def __init__(self):
        """Initialize."""
        pass
    
    def apply_overlap(
        self,
        current_chunk: Chunk,
        previous_chunk: Optional[Chunk]
    ) -> Chunk:
        """Return chunk unchanged."""
        return current_chunk


def create_overlap_strategy(config: Optional[OverlapConfig] = None) -> OverlapStrategy:
    """
    Factory function to create an overlap strategy.
    
    Args:
        config: Optional overlap configuration
    
    Returns:
        Configured overlap strategy
    """
    if config is None:
        config = OverlapConfig()
    
    if config.overlap_type == "character":
        return CharacterOverlapStrategy(config)
    elif config.overlap_type == "none":
        return NoOverlapStrategy()
    else:
        raise ValueError(f"Unknown overlap type: {config.overlap_type}")
