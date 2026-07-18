package com.pethats.menu;

import com.pethats.pet.HatAttributes;
import com.pethats.pet.PetInventoryContainer;
import net.minecraft.core.component.DataComponents;
import net.minecraft.world.Container;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.entity.EquipmentSlotGroup;
import net.minecraft.world.entity.LivingEntity;
import net.minecraft.world.entity.ai.attributes.AttributeInstance;
import net.minecraft.world.entity.ai.attributes.AttributeModifier;
import net.minecraft.world.entity.ai.attributes.Attributes;
import net.minecraft.world.entity.player.Inventory;
import net.minecraft.world.entity.player.Player;
import net.minecraft.world.inventory.AbstractContainerMenu;
import net.minecraft.world.inventory.Slot;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.item.component.ItemAttributeModifiers;
import net.minecraft.world.level.Level;
import org.jspecify.annotations.Nullable;

/**
 * The Ender Crown's pet inventory: a preview of the pet (rendered client-side by the screen), six storage
 * slots, and one weapon slot that only accepts items carrying an attack-damage attribute modifier.
 */
public class PetInventoryMenu extends AbstractContainerMenu {

	private static final int STORAGE_LEFT = 62;
	private static final int STORAGE_TOP = 18;
	private static final int WEAPON_X = 134;
	private static final int WEAPON_Y = 36;
	private static final int PLAYER_INV_TOP = 84;
	private static final int PLAYER_HOTBAR_TOP = 142;

	private final PetInventoryContainer petContainer;
	private final @Nullable LivingEntity pet;
	private ItemStack lastWeapon = ItemStack.EMPTY;

	public PetInventoryMenu(int containerId, Inventory playerInventory, @Nullable LivingEntity pet, PetInventoryContainer petContainer) {
		super(ModMenus.PET_INVENTORY, containerId);
		this.pet = pet;
		this.petContainer = petContainer;

		for (int row = 0; row < 2; row++) {
			for (int col = 0; col < 3; col++) {
				int index = row * 3 + col;
				addSlot(new Slot(petContainer, index, STORAGE_LEFT + col * 18, STORAGE_TOP + row * 18));
			}
		}
		addSlot(new WeaponSlot(petContainer, PetInventoryContainer.WEAPON_SLOT, WEAPON_X, WEAPON_Y));

		for (int row = 0; row < 3; row++) {
			for (int col = 0; col < 9; col++) {
				addSlot(new Slot(playerInventory, col + row * 9 + 9, 8 + col * 18, PLAYER_INV_TOP + row * 18));
			}
		}
		for (int col = 0; col < 9; col++) {
			addSlot(new Slot(playerInventory, col, 8 + col * 18, PLAYER_HOTBAR_TOP));
		}
	}

	/** Built server-side, where the pet entity and its live container are already known. */
	public static PetInventoryMenu forPet(int containerId, Inventory playerInventory, LivingEntity pet, PetInventoryContainer petContainer) {
		return new PetInventoryMenu(containerId, playerInventory, pet, petContainer);
	}

	/** Built client-side from the entity id sent by {@link net.fabricmc.fabric.api.menu.v1.ExtendedMenuType}. */
	public static PetInventoryMenu fromNetwork(int containerId, Inventory playerInventory, Integer petEntityId) {
		Level level = playerInventory.player.level();
		Entity entity = petEntityId == null ? null : level.getEntity(petEntityId);
		LivingEntity pet = entity instanceof LivingEntity living ? living : null;
		return new PetInventoryMenu(containerId, playerInventory, pet, new PetInventoryContainer());
	}

	public @Nullable LivingEntity pet() {
		return pet;
	}

	@Override
	public boolean stillValid(Player player) {
		return pet != null && pet.isAlive() && player.distanceToSqr(pet) <= 64.0;
	}

	@Override
	public void broadcastChanges() {
		super.broadcastChanges();
		if (pet == null || pet.level().isClientSide()) {
			return;
		}
		ItemStack currentWeapon = petContainer.getWeapon();
		if (!ItemStack.matches(currentWeapon, lastWeapon)) {
			lastWeapon = currentWeapon.copy();
			applyWeaponDamage(pet, currentWeapon);
		}
	}

	private static void applyWeaponDamage(LivingEntity pet, ItemStack weapon) {
		AttributeInstance damage = pet.getAttribute(Attributes.ATTACK_DAMAGE);
		if (damage == null) {
			return;
		}
		damage.removeModifier(HatAttributes.WEAPON_DAMAGE_ADD);
		double bonus = weaponAttackDamage(weapon);
		if (bonus != 0) {
			damage.addPermanentModifier(
					new AttributeModifier(HatAttributes.WEAPON_DAMAGE_ADD, bonus, AttributeModifier.Operation.ADD_VALUE));
		}
	}

	private static double weaponAttackDamage(ItemStack weapon) {
		if (weapon.isEmpty()) {
			return 0;
		}
		ItemAttributeModifiers modifiers = weapon.getOrDefault(DataComponents.ATTRIBUTE_MODIFIERS, ItemAttributeModifiers.EMPTY);
		double[] total = {0};
		modifiers.forEach(EquipmentSlotGroup.MAINHAND, (attribute, modifier) -> {
			if (attribute.equals(Attributes.ATTACK_DAMAGE) && modifier.operation() == AttributeModifier.Operation.ADD_VALUE) {
				total[0] += modifier.amount();
			}
		});
		return total[0];
	}

	@Override
	public ItemStack quickMoveStack(Player player, int index) {
		Slot slot = slots.get(index);
		if (slot == null || !slot.hasItem()) {
			return ItemStack.EMPTY;
		}

		ItemStack stackInSlot = slot.getItem();
		ItemStack result = stackInSlot.copy();
		int petSlotCount = PetInventoryContainer.TOTAL_SLOTS;

		if (index < petSlotCount) {
			if (!moveItemStackTo(stackInSlot, petSlotCount, slots.size(), true)) {
				return ItemStack.EMPTY;
			}
		} else if (!moveItemStackTo(stackInSlot, 0, PetInventoryContainer.STORAGE_SLOTS, false)
				&& !moveItemStackTo(stackInSlot, PetInventoryContainer.STORAGE_SLOTS, petSlotCount, false)) {
			return ItemStack.EMPTY;
		}

		if (stackInSlot.isEmpty()) {
			slot.setByPlayer(ItemStack.EMPTY);
		} else {
			slot.setChanged();
		}
		return result;
	}

	public static boolean hasAttackDamage(ItemStack stack) {
		ItemAttributeModifiers modifiers = stack.getOrDefault(DataComponents.ATTRIBUTE_MODIFIERS, ItemAttributeModifiers.EMPTY);
		boolean[] found = {false};
		modifiers.forEach(EquipmentSlotGroup.MAINHAND, (attribute, modifier) -> {
			if (attribute.equals(Attributes.ATTACK_DAMAGE)) {
				found[0] = true;
			}
		});
		return found[0];
	}

	private static final class WeaponSlot extends Slot {
		WeaponSlot(Container container, int index, int x, int y) {
			super(container, index, x, y);
		}

		@Override
		public boolean mayPlace(ItemStack stack) {
			return hasAttackDamage(stack);
		}
	}
}
