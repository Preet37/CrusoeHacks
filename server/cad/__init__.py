"""Twin CAD: LLM part plan -> validated -> OpenSCAD -> STL -> Blender -> GLB.

Plan/compile/render are adapted from LoominPhysicsSandbox (Preet37, pre-existing project);
see the header of each file. `attach(session)` hooks it into a meeting.
"""
from .hook import attach

__all__ = ["attach"]
