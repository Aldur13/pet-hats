package com.pethats.pet;

import com.pethats.item.HatItem;
import com.pethats.item.ModItems;
import com.pethats.menu.PetInventoryMenu;
import net.fabricmc.fabric.api.event.player.UseEntityCallback;
import net.fabricmc.fabric.api.menu.v1.ExtendedMenuProvider;
import net.minecraft.network.chat.Component;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.world.InteractionHand;
import net.minecraft.world.InteractionResult;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.entity.EquipmentSlot;
import net.minecraft.world.entity.LivingEntity;
import net.minecraft.world.entity.TamableAnimal;
import net.minecraft.world.entity.ai.attributes.AttributeInstance;
import net.minecraft.world.entity.ai.attributes.Attributes;
import net.minecraft.world.entity.animal.equine.AbstractHorse;
import net.minecraft.world.entity.player.Inventory;
import net.minecraft.world.entity.player.Player;
import net.minecraft.world.inventory.AbstractContainerMenu;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.level.Level;
import net.minecraft.world.phys.EntityHitResult;

/**
 * Handles right-clicking a tamed animal with a hat (equip/swap), with an empty hand while sneaking
 * (remove), or with an empty hand while an Ender Crown is worn (open the pet inventory).
 *
 * <p>All state changes happen only on the logical server; the client side of the event just reports the
 * same {@link InteractionResult} so vanilla's own handling (sitting, riding, etc.) is consistently
 * suppressed on both sides, and the client picks up the real result via the normal entity/inventory sync.
 */
public final class HatInteractionHandler {

	private HatInteractionHandler() {
	}

	public static void init() {
		UseEntityCallback.EVENT.register(HatInteractionHandler::onUseEntity);
	}

	private static InteractionResult onUseEntity(
			Player player, Level level, InteractionHand hand, Entity entity, EntityHitResult hitResult) {
		if (hand != InteractionHand.MAIN_HAND || !(entity instanceof LivingEntity pet) || !isTamedPet(entity)) {
			return InteractionResult.PASS;
		}

		ItemStack held = player.getItemInHand(hand);
		if (held.getItem() instanceof HatItem hatItem) {
			if (!level.isClientSide()) {
				equip(player, pet, hatItem, held);
			}
			return InteractionResult.SUCCESS;
		}

		if (held.isEmpty() && player.isShiftKeyDown()) {
			if (pet.getItemBySlot(EquipmentSlot.HEAD).isEmpty()) {
				return InteractionResult.PASS;
			}
			if (!level.isClientSide()) {
				remove(player, pet);
			}
			return InteractionResult.SUCCESS;
		}

		if (held.isEmpty() && !player.isShiftKeyDown() && pet.getItemBySlot(EquipmentSlot.HEAD).getItem() == ModItems.ENDER_CROWN) {
			if (!level.isClientSide() && player instanceof ServerPlayer serverPlayer) {
				openPetInventory(serverPlayer, pet);
			}
			return InteractionResult.SUCCESS;
		}

		return InteractionResult.PASS;
	}

	private static boolean isTamedPet(Entity entity) {
		if (entity instanceof TamableAnimal tamable) {
			return tamable.isTame();
		}
		if (entity instanceof AbstractHorse horse) {
			return horse.isTamed();
		}
		return false;
	}

	private static void equip(Player player, LivingEntity pet, HatItem hatItem, ItemStack held) {
		ItemStack previousHat = pet.getItemBySlot(EquipmentSlot.HEAD);
		pet.setItemSlot(EquipmentSlot.HEAD, held.copyWithCount(1));
		held.shrink(1);
		HatAttributes.apply(pet, hatItem.stats());

		if (!previousHat.isEmpty()) {
			returnHatToPlayer(player, pet, previousHat);
		}
	}

	private static void remove(Player player, LivingEntity pet) {
		ItemStack previousHat = pet.getItemBySlot(EquipmentSlot.HEAD);
		pet.setItemSlot(EquipmentSlot.HEAD, ItemStack.EMPTY);
		HatAttributes.clear(pet);
		returnHatToPlayer(player, pet, previousHat);
	}

	private static void returnHatToPlayer(Player player, LivingEntity pet, ItemStack hat) {
		if (hat.getItem() == ModItems.ENDER_CROWN) {
			dropPetInventory(pet);
		}
		if (!player.getInventory().add(hat)) {
			player.drop(hat, false);
		}
	}

	private static void dropPetInventory(LivingEntity pet) {
		if (!(pet.level() instanceof ServerLevel serverLevel)) {
			return;
		}
		PetInventoryContainer container = pet.getAttached(ModAttachments.PET_INVENTORY);
		if (container != null) {
			for (int i = 0; i < PetInventoryContainer.TOTAL_SLOTS; i++) {
				ItemStack stack = container.getItem(i);
				if (!stack.isEmpty()) {
					pet.spawnAtLocation(serverLevel, stack);
					container.setItem(i, ItemStack.EMPTY);
				}
			}
			pet.removeAttached(ModAttachments.PET_INVENTORY);
		}
		AttributeInstance damage = pet.getAttribute(Attributes.ATTACK_DAMAGE);
		if (damage != null) {
			damage.removeModifier(HatAttributes.WEAPON_DAMAGE_ADD);
		}
	}

	private static void openPetInventory(ServerPlayer player, LivingEntity pet) {
		PetInventoryContainer container = pet.getAttachedOrCreate(ModAttachments.PET_INVENTORY, PetInventoryContainer::new);
		player.openMenu(new ExtendedMenuProvider<Integer>() {
			@Override
			public Integer getScreenOpeningData(ServerPlayer serverPlayer) {
				return pet.getId();
			}

			@Override
			public Component getDisplayName() {
				return pet.getDisplayName();
			}

			@Override
			public AbstractContainerMenu createMenu(int containerId, Inventory inventory, Player p) {
				return PetInventoryMenu.forPet(containerId, inventory, pet, container);
			}
		});
	}
}
