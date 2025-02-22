#!/usr/bin/env python3

import dolfinx
import dolfiny
import numpy as np
import ufl
from mpi4py import MPI
from petsc4py import PETSc

import mesh_iso6892_gmshapi as mg

# Basic settings
name = "solid_plasticity_monolithic"
comm = MPI.COMM_WORLD

# Geometry and mesh parameters
l0, d0 = 0.10, 0.02  # [m]
nr = 5
# Geometry and physics ansatz order
o, p = 1, 1

# Create the mesh of the specimen with given dimensions
gmsh_model, tdim = mg.mesh_iso6892_gmshapi(name, l0, d0, nr, order=o)

# Create the mesh of the specimen with given dimensions and save as msh, then read into gmsh model
# mg.mesh_iso6892_gmshapi(name, l0, d0, nr, order=o, msh_file=f"{name}.msh")
# gmsh_model, tdim = dolfiny.mesh.msh_to_gmsh(f"{name}.msh")

# Get mesh and meshtags
mesh, mts = dolfiny.mesh.gmsh_to_dolfin(gmsh_model, tdim)

# Write mesh and meshtags to file
with dolfiny.io.XDMFFile(comm, f"{name}.xdmf", "w") as ofile:
    ofile.write_mesh_meshtags(mesh, mts)

# Read mesh and meshtags from file
with dolfiny.io.XDMFFile(comm, f"{name}.xdmf", "r") as ifile:
    mesh, mts = ifile.read_mesh_meshtags()

# Get merged MeshTags for each codimension
subdomains, subdomains_keys = dolfiny.mesh.merge_meshtags(mts, tdim - 0)
interfaces, interfaces_keys = dolfiny.mesh.merge_meshtags(mts, tdim - 1)

# Define shorthands for labelled tags
domain_gauge = subdomains_keys["domain_gauge"]
surface_1 = interfaces_keys["surface_grip_left"]
surface_2 = interfaces_keys["surface_grip_right"]

# Solid: material parameters
mu = dolfinx.fem.Constant(mesh, 100.0)  # [1e-9 * 1e+11 N/m^2 = 100 GPa]
la = dolfinx.fem.Constant(mesh, 10.00)  # [1e-9 * 1e+10 N/m^2 =  10 GPa]
Sy = dolfinx.fem.Constant(mesh, 0.300)  # initial yield stress [GPa]
bh = dolfinx.fem.Constant(mesh, 20.00)  # isotropic hardening: saturation rate  [-]
qh = dolfinx.fem.Constant(mesh, 0.100)  # isotropic hardening: saturation value [GPa]
bb = dolfinx.fem.Constant(mesh, 250.0)  # kinematic hardening: saturation rate  [-]
qb = dolfinx.fem.Constant(mesh, 0.100)  # kinematic hardening: saturation value [GPa] (includes factor 2/3)

# Solid: load parameters
μ = dolfinx.fem.Constant(mesh, 1.0)  # load factor


def u_bar(x):
    return μ.value * np.array([l0 * 0.01 * np.sign(x[0]), 0.0 * x[1], 0.0 * x[2]])


# Define integration measures
quad_degree = p
dx = ufl.Measure("dx", domain=mesh, subdomain_data=subdomains, metadata={"quadrature_degree": quad_degree})

# Function spaces
Ve = ufl.VectorElement("CG", mesh.ufl_cell(), p)
Te = ufl.TensorElement("Quadrature", mesh.ufl_cell(), degree=quad_degree, quad_scheme="default", symmetry=True)
Se = ufl.FiniteElement("Quadrature", mesh.ufl_cell(), degree=quad_degree, quad_scheme="default")

Vf = dolfinx.fem.FunctionSpace(mesh, Ve)
Tf = dolfinx.fem.FunctionSpace(mesh, Te)
Sf = dolfinx.fem.FunctionSpace(mesh, Se)

# Define functions
u = dolfinx.fem.Function(Vf, name="u")  # displacement
P = dolfinx.fem.Function(Tf, name="P")  # plastic strain
h = dolfinx.fem.Function(Sf, name="h")  # isotropic hardening
B = dolfinx.fem.Function(Tf, name="B")  # kinematic hardening

u0 = dolfinx.fem.Function(Vf, name="u0")
P0 = dolfinx.fem.Function(Tf, name="P0")
h0 = dolfinx.fem.Function(Sf, name="h0")
B0 = dolfinx.fem.Function(Tf, name="B0")

S0 = dolfinx.fem.Function(Tf, name="S0")

u_ = dolfinx.fem.Function(Vf, name="u_")  # boundary displacement

Po = dolfinx.fem.Function(dolfinx.fem.TensorFunctionSpace(mesh, ('DG', 0)), name="P")  # for output
Bo = dolfinx.fem.Function(dolfinx.fem.TensorFunctionSpace(mesh, ('DG', 0)), name="B")
So = dolfinx.fem.Function(dolfinx.fem.TensorFunctionSpace(mesh, ('DG', 0)), name="S")
ho = dolfinx.fem.Function(dolfinx.fem.FunctionSpace(mesh, ('DG', 0)), name="h")

δu = ufl.TestFunction(Vf)
δP = ufl.TestFunction(Tf)
δh = ufl.TestFunction(Sf)
δB = ufl.TestFunction(dolfinx.fem.FunctionSpace(mesh, Te))  # to be distinct from δP

# Define state and variation of state as (ordered) list of functions
m, δm = [u, P, h, B], [δu, δP, δh, δB]


def rJ2(A):
    """Square root of J2 invariant of tensor A"""
    J2 = 1 / 2 * ufl.inner(A, A)
    rJ2 = ufl.sqrt(J2)
    return ufl.conditional(rJ2 < 1.0e-12, 0.0, rJ2)


# Configuration gradient
I = ufl.Identity(u.geometric_dimension())  # noqa: E741
F = I + ufl.grad(u)  # deformation gradient as function of displacement

# Strain measures
E = 1 / 2 * (F.T * F - I)  # E = E(F), total Green-Lagrange strain
E_el = E - P  # E_el = E(F) - P, elastic strain

# Stress
S = 2 * mu * E_el + la * ufl.tr(E_el) * I  # S = S(E_el), PK2, St.Venant-Kirchhoff

# Wrap variable around expression (for diff)
S, B, h = ufl.variable(S), ufl.variable(B), ufl.variable(h)

# Yield function
f = ufl.sqrt(3) * rJ2(ufl.dev(S) - ufl.dev(B)) - (Sy + h)

# Plastic potential
g = f

# Total differential of yield function
df = + ufl.inner(ufl.diff(f, S), S - S0) \
     + ufl.inner(ufl.diff(f, h), h - h0) \
     + ufl.inner(ufl.diff(f, B), B - B0)

# Derivative of plastic potential wrt stress
dgdS = ufl.diff(g, S)

# Unwrap expression from variable
S, B, h = S.expression(), B.expression(), h.expression()

# Variation of Green-Lagrange strain
δE = dolfiny.expression.derivative(E, m, δm)

# Plastic multiplier (J2 plasticity: closed-form solution for return-map)
dλ = ufl.Max(f, 0)  # ppos = MacAuley bracket

# Weak form (as one-form)
F = + ufl.inner(δE, S) * dx \
    + ufl.inner(δP, (P - P0) - dλ * dgdS) * dx \
    + ufl.inner(δh, (h - h0) - dλ * bh * (qh * 1.00 - h)) * dx \
    + ufl.inner(δB, (B - B0) - dλ * bb * (qb * dgdS - B)) * dx

# Overall form (as list of forms)
F = dolfiny.function.extract_blocks(F, δm)

# Create output xdmf file -- open in Paraview with Xdmf3ReaderT
ofile = dolfiny.io.XDMFFile(comm, f"{name}.xdmf", "w")
# Write mesh, meshtags
ofile.write_mesh_meshtags(mesh, mts)

# Options for PETSc backend
opts = PETSc.Options(name)

opts["snes_type"] = "newtonls"
opts["snes_linesearch_type"] = "basic"
opts["snes_atol"] = 1.0e-12
opts["snes_rtol"] = 1.0e-09
opts["snes_max_it"] = 12
opts["ksp_type"] = "preonly"
opts["pc_type"] = "lu"
opts["pc_factor_mat_solver_type"] = "mumps"

# Create nonlinear problem: SNES
problem = dolfiny.snesblockproblem.SNESBlockProblem(F, m, prefix=name)

# Identify dofs of function spaces associated with tagged interfaces/boundaries
surface_1_dofs_Vf = dolfiny.mesh.locate_dofs_topological(Vf, interfaces, surface_1)
surface_2_dofs_Vf = dolfiny.mesh.locate_dofs_topological(Vf, interfaces, surface_2)

# Book-keeping of results
results = {'E': [], 'S': [], 'P': [], 'μ': []}

# Set up load steps
K = 25  # number of steps per load phase
Z = 2  # number of cycles
load, unload = np.linspace(0.0, 1.0, num=K + 1), np.linspace(1.0, 0.0, num=K + 1)
cycle = np.concatenate((load, unload, -load, -unload))
cycles = np.concatenate([cycle] * Z)

# Process load steps
for step, factor in enumerate(cycles):

    # Set current time
    μ.value = factor

    dolfiny.utils.pprint(f"\n+++ Processing load factor μ = {μ.value:5.4f}")

    # Update values for given boundary displacement
    u_.interpolate(u_bar)

    # Set/update boundary conditions
    problem.bcs = [
        dolfinx.fem.dirichletbc(u_, surface_1_dofs_Vf),  # disp left
        dolfinx.fem.dirichletbc(u_, surface_2_dofs_Vf),  # disp right
    ]

    # Solve nonlinear problem
    problem.solve()

    # Assert convergence of nonlinear solver
    assert problem.snes.getConvergedReason() > 0, "Nonlinear solver did not converge!"

    # Post-process data
    dxg = dx(domain_gauge)
    V = dolfiny.expression.assemble(1.0, dxg)
    n = ufl.as_vector([1, 0, 0])
    results['E'].append(dolfiny.expression.assemble(ufl.dot(E * n, n), dxg) / V)
    results['S'].append(dolfiny.expression.assemble(ufl.dot(S * n, n), dxg) / V)
    results['P'].append(dolfiny.expression.assemble(ufl.dot(P * n, n), dxg) / V)
    results['μ'].append(factor)

    # Basic consistency checks
    assert dolfiny.expression.assemble(dλ * df, dxg) / V < 1.e-03, "|| dλ*df || != 0.0"
    assert dolfiny.expression.assemble(dλ * f, dxg) / V < 1.e-06, "|| dλ*df || != 0.0"

    # Fix: 2nd order tetrahedron
    # mesh.geometry.cmap.non_affine_atol = 1.0e-8
    # mesh.geometry.cmap.non_affine_max_its = 20

    # Write output
    ofile.write_function(u, step)

    # Project and write output
    dolfiny.projection.project(P, Po)
    dolfiny.projection.project(B, Bo)
    dolfiny.projection.project(S, So)
    dolfiny.projection.project(h, ho)
    ofile.write_function(Po, step)
    ofile.write_function(Bo, step)
    ofile.write_function(So, step)
    ofile.write_function(ho, step)

    # Store stress state
    dolfiny.interpolation.interpolate(S, S0)

    # Store primal states
    for source, target in zip([u, P, h, B], [u0, P0, h0, B0]):
        with source.vector.localForm() as locs, target.vector.localForm() as loct:
            locs.copy(loct)

ofile.close()

# Post-process results

import matplotlib.pyplot

fig, ax1 = matplotlib.pyplot.subplots()
ax1.set_title("Rate-independent plasticity: $J_2$, monolithic formulation, 3D", fontsize=12)
ax1.set_xlabel(r'volume-averaged strain $\frac{1}{V}\int n^T E n \, dV$ [-]', fontsize=12)
ax1.set_ylabel(r'volume-averaged stress $\frac{1}{V}\int n^T S n \, dV$ [GPa]', fontsize=12)
ax1.grid(linewidth=0.25)
fig.tight_layout()

E = np.array(results['E'])
S = np.array(results['S'])

# stress-strain curve
ax1.plot(E, S, color='tab:blue', linestyle='-', linewidth=1.0, markersize=4.0, marker='.', label=r'$S-E$ curve')

ax1.legend(loc='lower right')
ax1.ticklabel_format(style='sci', scilimits=(-2, -2), axis='x')
fig.savefig(f"{name}.pdf")
