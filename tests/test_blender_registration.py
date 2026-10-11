"""Registration must survive dialog invocation followed by calculation launch."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from metrosim26 import blender_ui


class RegistrationTests(unittest.TestCase):
    def test_operator_registered_once_without_bpy_types_alias(self):
        operator = SimpleNamespace(is_registered=False)
        register = Mock(side_effect=lambda cls: setattr(cls, "is_registered", True))
        fake_bpy = SimpleNamespace(
            types=SimpleNamespace(), utils=SimpleNamespace(register_class=register)
        )
        with (
            patch.object(blender_ui, "bpy", fake_bpy),
            patch.object(blender_ui, "METROSIM26_OT_extend", operator, create=True),
        ):
            blender_ui._register_extension_operator()
            blender_ui._register_extension_operator()
        register.assert_called_once_with(operator)
