package com.pethats.pet;

import com.pethats.PetHats;
import com.pethats.item.HatStats;
import net.minecraft.core.Holder;
import net.minecraft.resources.Identifier;
import net.minecraft.world.entity.LivingEntity;
import net.minecraft.world.entity.ai.attributes.Attribute;
import net.minecraft.world.entity.ai.attributes.AttributeInstance;
import net.minecraft.world.entity.ai.attributes.AttributeModifier;
import net.minecraft.world.entity.ai.attributes.Attributes;

/** Applies and removes the {@link HatStats} attribute modifiers a worn hat grants its pet. */
public final class HatAttributes {

	private static final Identifier HEALTH_ADD = id("hat_health_add");
	private static final Identifier HEALTH_MULT = id("hat_health_mult");
	private static final Identifier DAMAGE_ADD = id("hat_damage_add");
	private static final Identifier DAMAGE_MULT = id("hat_damage_mult");
	private static final Identifier ARMOR_ADD = id("hat_armor_add");
	private static final Identifier SPEED_ADD = id("hat_speed_add");
	/** The Ender Crown pet inventory's weapon-slot damage bonus; independent of the worn-hat modifiers above. */
	public static final Identifier WEAPON_DAMAGE_ADD = id("weapon_damage_add");

	private HatAttributes() {
	}

	private static Identifier id(String path) {
		return Identifier.fromNamespaceAndPath(PetHats.MOD_ID, path);
	}

	/** Removes any previous hat's modifiers, then applies {@code stats}'s modifiers and tops off health. */
	public static void apply(LivingEntity pet, HatStats stats) {
		clear(pet);

		AttributeInstance health = pet.getAttribute(Attributes.MAX_HEALTH);
		if (health != null) {
			if (stats.healthAdd() != 0) {
				health.addPermanentModifier(new AttributeModifier(HEALTH_ADD, stats.healthAdd(), AttributeModifier.Operation.ADD_VALUE));
			}
			if (stats.healthMultiplier() != 1.0) {
				health.addPermanentModifier(
						new AttributeModifier(HEALTH_MULT, stats.healthMultiplier() - 1.0, AttributeModifier.Operation.ADD_MULTIPLIED_TOTAL));
			}
		}

		AttributeInstance damage = pet.getAttribute(Attributes.ATTACK_DAMAGE);
		if (damage != null) {
			if (stats.damageAdd() != 0) {
				damage.addPermanentModifier(new AttributeModifier(DAMAGE_ADD, stats.damageAdd(), AttributeModifier.Operation.ADD_VALUE));
			}
			if (stats.damageMultiplier() != 1.0) {
				damage.addPermanentModifier(
						new AttributeModifier(DAMAGE_MULT, stats.damageMultiplier() - 1.0, AttributeModifier.Operation.ADD_MULTIPLIED_TOTAL));
			}
		}

		AttributeInstance armor = pet.getAttribute(Attributes.ARMOR);
		if (armor != null && stats.armorAdd() != 0) {
			armor.addPermanentModifier(new AttributeModifier(ARMOR_ADD, stats.armorAdd(), AttributeModifier.Operation.ADD_VALUE));
		}

		AttributeInstance speed = pet.getAttribute(Attributes.MOVEMENT_SPEED);
		if (speed != null && stats.speedPercent() != 0) {
			speed.addPermanentModifier(new AttributeModifier(SPEED_ADD, stats.speedPercent(), AttributeModifier.Operation.ADD_MULTIPLIED_TOTAL));
		}

		// A freshly equipped hat tops the pet off, so a bigger max health pool is felt immediately.
		pet.setHealth(pet.getMaxHealth());
	}

	/** Strips every modifier a hat could have applied, leaving the pet's base stats untouched. */
	public static void clear(LivingEntity pet) {
		removeIfPresent(pet, Attributes.MAX_HEALTH, HEALTH_ADD);
		removeIfPresent(pet, Attributes.MAX_HEALTH, HEALTH_MULT);
		removeIfPresent(pet, Attributes.ATTACK_DAMAGE, DAMAGE_ADD);
		removeIfPresent(pet, Attributes.ATTACK_DAMAGE, DAMAGE_MULT);
		removeIfPresent(pet, Attributes.ARMOR, ARMOR_ADD);
		removeIfPresent(pet, Attributes.MOVEMENT_SPEED, SPEED_ADD);
		if (pet.getHealth() > pet.getMaxHealth()) {
			pet.setHealth(pet.getMaxHealth());
		}
	}

	private static void removeIfPresent(LivingEntity pet, Holder<Attribute> attribute, Identifier modifierId) {
		AttributeInstance instance = pet.getAttribute(attribute);
		if (instance != null) {
			instance.removeModifier(modifierId);
		}
	}
}
