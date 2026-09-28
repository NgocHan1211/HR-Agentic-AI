"""Human-approved, structured agent workflow primitives.

This package deliberately contains no UI or cloud-provider code.  The same domain
layer can run behind the current FastAPI application locally and behind a
Cloudflare Worker/Workflow adapter in deployment.
"""

from .models import CaseStatus, PlanProposal

__all__ = ["CaseStatus", "PlanProposal"]
