from marshmallow import Schema, fields, validate, pre_load, ValidationError

import ipaddress


class NETunnelSchema(Schema):
	"""
	Base schema for netunnel objects

	For backwards compatibility to marshmallow 2.X, we're using self.dump3 and self.load3 instead
	of self.dump & self.load to make them as similar as possible to marshmallow 3.X.
	We're not overriding these methods directly to avoid breaking marshmallow as they're being used internally.
	"""
	def dump3(self, obj, *args, **kwargs):
		dump_result = super().dump(obj, *args, **kwargs)
		# We cannot use isinstance because MarshalResult won't exists on marshmallow 3.X
		if type(dump_result).__name__ == 'MarshalResult':
			if dump_result.errors:
				raise ValidationError(message=dump_result.errors)
			return dump_result.data
		return dump_result

	def load3(self, obj, *args, **kwargs):
		load_result = super().load(obj, *args, **kwargs)
		# We cannot use isinstance because UnmarshalResult won't exists on marshmallow 3.X
		if type(load_result).__name__ == 'UnmarshalResult':
			if load_result.errors:
				raise ValidationError(message=load_result.errors)
			return load_result.data
		return load_result


class StaticTunnelSchema(NETunnelSchema):
	id = fields.Integer()
	tunnel_remote_address = fields.String(default='127.0.0.1', missing='127.0.0.1')
	tunnel_remote_port = fields.Integer(required=True, validate=validate.Range(min=1, max=65535))
	tunnel_local_address = fields.String(default='127.0.0.1', missing='127.0.0.1')
	tunnel_local_port = fields.Integer(required=True, validate=validate.Range(min=1, max=65535))

	@pre_load
	def validate_scheme(self, data, **kwargs):
		for address in ['tunnel_remote_address', 'tunnel_local_address']:
			try:
				ipaddress.IPv4Address(data.get(address, '127.0.0.1'))
			except ipaddress.AddressValueError as err:
				raise ValidationError(str(err))
		return data


class PeerSchema(NETunnelSchema):
	id = fields.Integer()
	name = fields.String(required=True)
	target_netunnel_url = fields.URL(required=True)
	auth_data = fields.Dict(required=True, keys=fields.String(), values=fields.String())
	static_tunnels = fields.List(fields.Nested(StaticTunnelSchema))
