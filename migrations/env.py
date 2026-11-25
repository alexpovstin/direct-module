from alembic import context

from direct_module.config import Settings
from direct_module.db import open_engine
from direct_module.models import Base

settings = Settings.from_env()

if context.is_offline_mode():
    context.configure(
        url="sqlite://",
        target_metadata=Base.metadata,
        literal_binds=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = open_engine(settings.database)
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=Base.metadata,
                render_as_batch=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()
