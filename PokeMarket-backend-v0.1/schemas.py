from typing import Any
from pydantic import BaseModel,Field
class HealthResponse(BaseModel):
    status:str;service:str;environment:str;version:str;ai_provider:str
class CardSearchResponse(BaseModel):
    query:str;cards:list[dict[str,Any]]
class AIAnalysisResult(BaseModel):
    identification:dict[str,Any];condition:dict[str,Any];authenticity:dict[str,Any];warnings:list[str]=Field(default_factory=list)
class ScanAnalysisResponse(BaseModel):
    scan_id:str;status:str;provider:str;identification:dict[str,Any];condition:dict[str,Any];authenticity:dict[str,Any];tcgdex:dict[str,Any]|None=None;warnings:list[str]=Field(default_factory=list)
