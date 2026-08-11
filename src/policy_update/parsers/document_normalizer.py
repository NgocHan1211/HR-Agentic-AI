from __future__ import annotations
import re
import unicodedata
from dataclasses import replace

from .base_parser import ContentBlock, ParsedDocument

class DocumentNormalizer:
    """
    Normalize parser output into a consistent representation.

    Responsibilities:
    - Unicode normalization
    - Whitespace normalization
    - Normalize line breaks
    - Clean empty blocks
    - Normalize block ordering
    - Generate stable block IDs when necessary
    - Preserve raw_text
    """
    
    def normalize(self, document: ParsedDocument) -> ParsedDocument:
        """
        Normalize a ParsedDocument.
        The original document is not mutated.
        """
        
        normalized_blocks: list[ContentBlock] = []
        
        for index, block in enumerate(document.blocks):
            normalized = self.normalize_block(block = block, fallback_order = index)
            
            if normalized is None:
                continue
            
            normalized_blocks.append(normalized)
            
        normalized_blocks = self._reorder_blocks(normalized_blocks)
        normalized_blocks = self._ensure_unique_block_ids(normalized_blocks)
        
        return replace(document, blocks=normalized_blocks)
    
    def normalize_block(self, block: ContentBlock, fallback_order: int = 0) -> ContentBlock | None:
        """
        Normalize one ContentBlock.
        Returns None if the block contains no meaningful text.
        """
        
        raw_text = block.raw_text or ""
        normalized_text = self.normalize_text(raw_text)
        
        if not normalized_text:
            return None
        
        block_id = self._normalize_block_id(block.block_id, fallback_order)
        order = block.order if block.order is not None else fallback_order
        
        return replace(block, block_id=block_id, normalized_text=normalized_text, order=order)
    
    @staticmethod
    def normalize_text(text: str) -> str:
        """
        Normalize text without changing its semantic meaning.

        Steps:
        1. Unicode NFKC normalization
        2. Normalize line endings
        3. Remove zero-width/control characters
        4. Normalize spaces
        5. Normalize blank lines
        6. Strip leading/trailing whitespace
        """
        
        if not text:
            return ""
        
        # 1. Unicode NFKC normalization
        text = unicodedata.normalize("NFKC", text)
        # 2. Normalize line endings
        text = text.replace("\r\n", "\n")
        text = text.replace("\r", "\n")
        # 3. Remove zero-width/control characters
        text = re.sub(r"[\u200B\u200C\u200D\uFEFF]", "", text)
        text = "".join(char for char in text if char in ("\n", "\t") or not unicodedata.category(char).startswith("C"))
        # 4. Normalize spaces
        text = text.replace("\t", " ")
        text = re.sub(r"[ ]{2,}", " ", text)
        text = re.sub(r"[ ]+\n", "\n", text)
        text = re.sub(r"\n[ ]+", "\n", text)
        # 5. Normalize blank lines
        text = re.sub(r"\n{3,}", "\n\n", text)
        # 6. Strip leading/trailing whitespace
        return text.strip()
    
    @staticmethod
    def _normalize_block_id(block_id: str | None, fallback_order: int) -> str:
        """
        Keep an existing block ID if valid. Otherwise create a deterministic fallback ID.
        Parser-specific IDs should normally already exist.
        """
        
        if block_id and block_id.strip():
            return block_id.strip()

        return f"block-{fallback_order:06d}"
    
    @staticmethod
    def _reorder_blocks(blocks: list[ContentBlock]) -> list[ContentBlock]:
        """
        Sort blocks by parser-provided order.
        Do not try to infer document structure here.
        """

        return sorted(blocks, key=lambda block: block.order,)
        
    @staticmethod
    def _ensure_unique_block_ids(blocks: list[ContentBlock]) -> list[ContentBlock]:
        """
        Ensure block IDs are unique within one ParsedDocument.
        If a parser accidentally produces duplicate IDs, append a numeric suffix.
        """

        seen: dict[str, int] = {}
        result: list[ContentBlock] = []

        for block in blocks:
            original_id = block.block_id

            if original_id not in seen:
                seen[original_id] = 0
                result.append(block)
                continue

            seen[original_id] += 1
            new_id = (f"{original_id}-{seen[original_id]}")
            result.append(replace(block, block_id=new_id))

        return result
    
def normalize_document(document: ParsedDocument,) -> ParsedDocument:
    """Convenience function for parser/service layer."""
        
    return DocumentNormalizer().normalize(document)