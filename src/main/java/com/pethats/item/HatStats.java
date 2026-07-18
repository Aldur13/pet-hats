package com.pethats.item;

/**
 * The gameplay buffs granted by a hat while it is worn.
 *
 * <p>Flat bonuses ({@code healthAdd}, {@code damageAdd}, {@code armorAdd}, {@code speedPercent}) are
 * applied as {@code ADD_VALUE}/{@code ADD_MULTIPLIED_TOTAL} attribute modifiers. {@code healthMultiplier}
 * and {@code damageMultiplier} (only used by the Ender Crown) multiply the pet's final health/damage.
 */
public record HatStats(
		double healthAdd,
		double damageAdd,
		double armorAdd,
		double speedPercent,
		double healthMultiplier,
		double damageMultiplier,
		boolean unlocksPetInventory) {

	public static HatStats flat(double healthAdd, double damageAdd) {
		return new HatStats(healthAdd, damageAdd, 0, 0, 1.0, 1.0, false);
	}

	public static HatStats flat(double healthAdd, double damageAdd, double armorAdd, double speedPercent) {
		return new HatStats(healthAdd, damageAdd, armorAdd, speedPercent, 1.0, 1.0, false);
	}

	public static HatStats multiplier(double healthMultiplier, double damageMultiplier, boolean unlocksPetInventory) {
		return new HatStats(0, 0, 0, 0, healthMultiplier, damageMultiplier, unlocksPetInventory);
	}
}
