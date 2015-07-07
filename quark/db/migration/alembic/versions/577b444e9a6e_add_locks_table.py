"""Add locks table

Revision ID: 577b444e9a6e
Revises: 5932938bb839
Create Date: 2015-07-07 10:42:47.671194

"""

# revision identifiers, used by Alembic.
revision = '577b444e9a6e'
down_revision = '5932938bb839'

from alembic import op
import sqlalchemy as sa


def upgrade():
    op.create_table('quark_locks',
                    sa.Column('created_at', sa.DateTime(), nullable=True),
                    sa.Column('id', sa.Integer(), nullable=False),
                    sa.Column('type', sa.Enum('ip_address'), nullable=False),
                    sa.Column('name', sa.String(length=255), nullable=True),
                    sa.PrimaryKeyConstraint('id'),
                    mysql_engine='InnoDB')
    op.add_column(u'quark_ip_addresses',
                  sa.Column('lock_id', sa.Integer(), nullable=True))


def downgrade():
    op.drop_column(u'quark_ip_addresses', 'lock_id')
    op.drop_table('quark_locks')
