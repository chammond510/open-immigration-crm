"""The firm's staff: the Team page, invitations, roles, and leaving the firm.

People who may manage staff add a person with a name, email, and role. The new
account has no usable password; the person chooses one through an invitation
link, emailed through the firm's system email connection when there is one and
otherwise shown once to whoever added them. Nobody can give a role with a
permission they do not hold, or change someone who holds a permission they do
not, so managing staff never widens anyone's own access. Deactivating someone
hands their open work, matters, and contacts to a colleague. Anyone who forgets
their password can ask for a link by email when the firm can send it (reset.py).
"""
