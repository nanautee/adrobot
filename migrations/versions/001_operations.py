from alembic import op
import sqlalchemy as sa

revision = '001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('operations',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('kind', sa.String(40), nullable=False),
        sa.Column('campaign_id', sa.Integer(), nullable=False),
        sa.Column('details', sa.String(500), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))


def downgrade():
    op.drop_table('operations')
