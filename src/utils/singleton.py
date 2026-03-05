"""Singleton pattern factories for services"""


def create_singleton_factory(cls, name: str, init_method: str = None):
    """Create a singleton factory function"""
    instance = None

    def factory(**kwargs):
        nonlocal instance
        if instance is None:
            instance = cls(**kwargs)
            if init_method:
                import asyncio
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(getattr(instance, init_method)())
                finally:
                    loop.close()
        return instance

    factory.__name__ = name
    return factory


def create_async_singleton_factory(cls, name: str, init_method: str = None):
    """Create an async singleton factory function"""
    instance = None
    initialized = False

    async def factory(**kwargs):
        nonlocal instance, initialized
        if instance is None:
            instance = cls(**kwargs)
        if not initialized and init_method:
            await getattr(instance, init_method)()
            initialized = True
        return instance

    factory.__name__ = name
    return factory
